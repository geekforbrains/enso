# Web UI

The local dashboard: pages, routes, and read/write behaviour. Server-rendered, no chat.
See [architecture.md](architecture.md) for how the server runs and is secured, and
[data-model.md](data-model.md) for what it reads/writes.

## Shape

A small **server-rendered** app (Starlette + Jinja2). There is no SPA, runtime build, or
external CDN: compiled CSS is vendored under `web/static/`. Every navigable URL returns a
complete document, and forms and links use ordinary browser requests and redirects.

The whole UI is a thin skin over the running process's active configuration, the file model,
the shared DB, and the existing Slack directory cache. Pages read workspace and policy
bindings, Slack routes, `JOB.md` / `SKILL.md` / `AGENTS.md`, run history, and registered user
tables. Writes go straight back to owned files (atomic replace) and the run store;
configuration, policies, Slack routes, and user-table pages are read-only. There is no
separate web database or cache (see [data-model.md](data-model.md)).

The app factory validates the local Git root and canonical scaffold before serving. That
check is read-only: the web process never seeds prompts or skills, creates workspaces,
repairs links, or changes setup state.

**Write boundary.** Every write the UI makes lands inside `~/.enso/` (jobs, Enso-owned
skills, the canonical shared `AGENTS.md`, and root `AGENTS.md` files under the managed
`~/.enso/workspaces/` tree). External workspace roots are invalid; nested workspace
instruction files, native policy files, and external "parent" skills discovered from the
CLIs' own roots (e.g. `~/.claude/skills/`) are strictly read-only. This is both the safety
boundary and the ownership model: Enso manages its own files and only observes the rest.

**Request protection.** Host headers must match loopback, the concrete bind host, or a
name/IP in `web.allowed_hosts`; wildcard binds do not disable this check. All POST routes
also require a random, process-scoped CSRF token supplied by the rendered form or an
`X-CSRF-Token` header. Responses deny framing, disable MIME sniffing, use a no-referrer
policy, and prevent HTML caching. Host filtering is not authentication: an empty
`web.token`, with no `web.token_1password` reference, accepts every reachable client.
`web.token_1password` uses the same `{item, field}` shape as transport credentials and
is resolved through `~/.enso/lib/1password.sh` when the app is created. The resolved
token stays process-local. A present reference takes precedence over `web.token` and
fails app creation if it is malformed, unavailable, or empty. A configured literal
`web.token` must be a string; malformed JSON types also fail app creation.

## Routes

| Route                                      | Method    | Status      | Purpose                                                              |
| ------------------------------------------ | --------- | ----------- | -------------------------------------------------------------------- |
| `/`                                        | GET       | Implemented | Dashboard — execution configuration plus recent operational activity |
| `/workspaces`                              | GET       | Implemented | Active workspace catalog, policies, bindings, and status             |
| `/workspaces/{name}`                       | GET       | Implemented | One workspace, root editor, child instructions, routes, and jobs     |
| `/workspaces/{name}/agents/edit`           | POST      | Implemented | Revision-checked save of one managed root `AGENTS.md`                 |
| `/workspaces/{name}/agents/{path:path}`    | GET       | Implemented | Read-only view of one existing nested `AGENTS.md`                    |
| `/policies`                                | GET       | Implemented | Active reusable policy catalog and consuming workspaces              |
| `/policies/{name}`                         | GET       | Implemented | Normalized policy configuration and provider validation status       |
| `/slack`                                   | GET       | Implemented | Exact Slack routes with cached labels and derived policy bindings     |
| `/health`                                  | GET       | Implemented | Unauthenticated process-health probe                                 |
| `/jobs`                                    | GET       | Implemented | Job list — schedule, provider/model, workspace, enabled state        |
| `/jobs/new`             | GET, POST | **Planned** | Create-job form and `JOB.md` scaffold                                |
| `/jobs/{name}`          | GET       | Implemented | Job configuration, prompt, prerun state, and recent runs             |
| `/jobs/{name}/edit`     | POST      | **Planned** | Edit job metadata and prerun configuration                           |
| `/jobs/{name}/prompt`   | POST      | Implemented | Edit only the prompt body while preserving raw frontmatter           |
| `/jobs/{name}/prerun`   | POST      | Implemented | Edit the configured prerun script while preserving its mode          |
| `/jobs/{name}/toggle`   | POST      | Implemented | Enable or disable a job                                              |
| `/jobs/{name}/delete`   | POST      | Implemented | Delete a job directory after confirmation                            |
| `/jobs/{name}/run`      | POST      | Implemented | Run now and record a `manual` run                                    |
| `/runs`                 | GET       | Implemented | Paginated run feed; filter by `?name=`, `?status=`                   |
| `/runs/{id}`            | GET       | Implemented | Run metadata and captured log output                                 |
| `/skills`               | GET       | Implemented | Enso-owned and external read-only skill tiers                        |
| `/skills/new`           | GET, POST | **Planned** | Create an Enso-owned skill                                           |
| `/skills/{name}`        | GET       | Implemented | View `SKILL.md`; edit controls appear for Enso-owned skills          |
| `/skills/{name}/edit`   | POST      | Implemented | Replace an Enso-owned skill's `SKILL.md`                             |
| `/skills/{name}/delete` | POST      | Implemented | Delete an Enso-owned skill directory after confirmation              |
| `/docs`                 | GET       | Implemented | Reference-doc list — name, description, relative path                |
| `/docs/new`             | GET, POST | Implemented | Create a doc and scaffold its frontmatter                            |
| `/docs/{path:path}`     | GET       | Implemented | View and edit one doc                                                |
| `/docs/edit`            | POST      | Implemented | Replace a doc's contents atomically                                  |
| `/docs/delete`          | POST      | Implemented | Delete a doc after confirmation                                      |
| `/tables`               | GET       | Implemented | Registered data-table list with discovery metadata                   |
| `/tables/{name}`        | GET       | Implemented | Schema summary and bounded, read-only row preview                    |
| `/agents`                                  | GET       | Implemented | View the canonical shared `~/.enso/AGENTS.md`                        |
| `/agents/edit`                             | POST      | Implemented | Revision-checked save of the shared `AGENTS.md`                      |
| `/static/*`             | GET       | Implemented | Vendored CSS and image assets                                        |

Every page request returns a complete document. Successful writes use ordinary `303`
redirects to their resulting page. Table pagination and the Runs browser use normal links
and GET forms, keeping filter and page state in shareable URLs.

## Pages

### Dashboard (`/`)

The dashboard shows:

- **Execution configuration** — workspace, policy, and Slack-route totals plus one visible
  configured/warning/error state derived from the active process configuration. Configured
  means the binding is structurally valid, not that protected native policy files have been
  checked; policy detail upgrades a successful native validation to ready. Links open the
  corresponding configuration pages.
- **Recent runs** — the last six rows from `runs`, newest first: kind, name,
  status pill (running/ok/error/timeout/prerun error/prerun timeout), trigger, duration,
  relative time; each links to `/runs/{id}`. A database read failure is shown explicitly
  as **Database busy** or **Database unavailable** instead of looking like an empty run
  history.
- **Jobs enabled** — the enabled and total job counts, linking to the job list.
- **Skills** — deduplicated Enso-owned and visible system counts, linking to the skill
  list.
- **Docs** — the reference-doc count, linking to the doc list.
- **Tables** — available registered user-table count, linking to the read-only table list. A database read failure is shown as **Database busy** or **Database unavailable**, never as a misleading zero.

### Workspaces (`/workspaces`, `/workspaces/{name}`)

Workspaces are the execution roots selected by Slack routes, Telegram, and jobs. The list
and detail pages render the configuration held by the running dashboard process; they do
not reload or modify `config.json`, and a disk edit takes effect only after the relevant
service is restarted.

- The list shows the canonical name-derived path, exactly one linked policy, concurrency, Slack-route
  and job counts, Telegram binding, instruction-file count, and all structural problems.
- Detail shows the same binding plus associated routes and jobs. Every lowercase
  kebab-case name resolves exactly to `~/.enso/workspaces/<name>`; configuration cannot
  provide another path. A symlinked container/root, a direct root `.git` entry, an
  incorrect discovery link, or a duplicate global/workspace skill name is an error.
- A bounded no-symlink scan discovers exact `AGENTS.md` names to a maximum depth of six,
  100 files, 2,000 directories, and 20,000 directory entries. Dot directories and common
  generated roots such as `node_modules`, `vendor`, `dist`, `build`, `target`, `uploads`,
  and `runtime` are pruned. Reaching a bound is visible rather than silently implying the
  inventory is complete.
- Existing nested files have read-only detail pages. Only the root `AGENTS.md` of a managed
  workspace can be created or edited in the browser; `CLAUDE.md` is never traversed or
  replaced.

Shared and managed workspace edits use the same hardened file boundary. Every path
component is opened relative to pinned directory descriptors with no symlink following;
files must be current-user-owned regular files with one link and no group/other write bit.
Reads are stable, bounded UTF-8 with no NUL. The form carries a SHA-256 revision, and the
save uses an atomic name exchange while rechecking the target identity, revision, and staged
bytes through its final in-operation verification. A conflicting revision or stable one-shot
race returns `409` and rolls back without discarding the competing bytes. Continuous mutation
can make a verified rollback impossible; that fails closed as `503` and leaves uncertain
objects intact for operator recovery. No filesystem API can prevent another same-user process
from changing the file after the save has completed. Unsafe paths/integrity return `403`,
missing files `404`, oversized submissions `413`, invalid text `422`, and unavailable secure
filesystem operations `503` without exposing raw exceptions.

### Policies (`/policies`, `/policies/{name}`)

A policy is a reusable authority selected by one or more workspaces. These pages are
strictly read-only. They show unrestricted versus policy-controlled mode, allowed/default
providers, chat commands, environment-variable names (never values), the protected policy
directory, and consuming workspaces. Detail runs the same native provider validation used
by configuration checks for each consuming workspace and displays safe problems, warnings,
revision digests, policy paths, and MCP server names. It never renders native policy file
contents, staged runtime credentials, transport secrets, or raw MCP configuration. An
unused policy is shown explicitly and is not launched merely for a web request.

Catalog lists label structurally valid bindings as **configured**, not ready. Native policy
files are intentionally checked only on policy detail, where successful checks are labeled
**ready** and failures or warnings replace that status. This avoids both filesystem scans on
every dashboard request and a false claim that a protected launch was validated.

### Slack routes (`/slack`)

The Slack page shows each exact DM-user or channel ID, its workspace and derived policy,
audit state, mention/thread triggers, and effective configuration errors. Friendly names
come only from the existing `~/.enso/cache/slack.json` directory cache; a page request
never resolves credentials or calls Slack. Missing names fall back to exact IDs, cached
user emails are never rendered, and labels are accepted only when the cache's account ID
exactly matches the configured account. The Slack transport binds that cache after
`auth.test` and clears unbound or foreign directory entries before refreshing them; an
unbound or mismatched cache is ignored by the web UI and shown as a warning.

### Jobs (`/jobs`, `/jobs/{name}`)

- Read: schedule, provider/model, required workspace name, timeout, notify destination, prompt body, and whether
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
- Recent runs for this job, linking to `/runs/{id}`. If run history is busy or unavailable,
  the configuration and editors remain usable and the failure is shown only in the Runs card.
- **Planned:** browser forms for create and full metadata editing, including choosing the workspace and prerun path. The workspace's configured policy governs execution. Until then use `enso job create` or edit the job files directly.

### Run detail (`/runs/{id}`)

- Metadata: kind, name/title, trigger, provider/model, status, exit code, start/end,
  duration.
- Output: up to the first 200,000 bytes of `runs/{id}.log`, monospace and wrapped. If
  truncated, the page shows the full byte count and on-disk path.
- A run with no captured output displays an empty-state message.

The `/runs` feed fetches at most 51 records to render a 50-record page and determine
whether a next page exists without `COUNT(*)`. Filters and page state stay in the URL,
and each request returns the full page. Each record has one responsive DOM representation
rather than separate hidden mobile and desktop copies.

Run list/detail reads execute outside the ASGI event loop with a 500 ms SQLite busy
timeout. A timeout returns a retryable full-page `503` **Database busy** state; open,
permission, corruption, and other access failures return a full-page `503` **Database
unavailable** state. Neither response includes the raw exception text.

### Skills (`/skills`, `/skills/{name}`)

Two tiers, split by the `~/.enso/` write boundary:

- **Enso skills** — everything under `~/.enso/skills/`, whether created here or copied
  once by fresh setup. Listed with name + description (from SKILL.md
  frontmatter). `/skills/{name}` offers whole-file `SKILL.md` editing and confirmed
  directory deletion. Installed copies are user-owned: startup, upgrades, and setup repair
  do not seed missing bundle entries, advance pristine copies, create deletion markers,
  resurrect deleted skills, or remove guessed tool copies.
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
- Previous/Next links perform ordinary full-page navigation to a shareable page URL

The page uses a short-lived SQLite connection outside the ASGI event loop with a 500 ms
busy timeout. It returns a full-page `503` with **Database busy** for lock contention or
**Database unavailable** for other access failures. The route exposes no write operation,
SQL input, filter/sort expression, schema control, row editing, or delete action. The
catalog, CLI, agent workflow, and failure semantics are specified in [tables.md](tables.md).

`/health` is a process-liveness probe and never opens SQLite. A database request waiting
for its bounded timeout therefore cannot delay health checks or unrelated web requests.

### AGENTS.md (`/agents`)

- Renders the canonical shared instructions at `~/.enso/AGENTS.md`, which Enso injects into every workspace launch.
- **Editable**: a textarea + save, POST to `/agents/edit`, with the same owner/type/link,
  stable-read, size, UTF-8/NUL, revision-conflict, and atomic-replace checks as managed
  workspace roots. The sibling `CLAUDE.md -> AGENTS.md` symlink is left intact because the
  editor addresses only the canonical `AGENTS.md` regular file. Workspace-local focused
  instructions have their own workspace pages.

## Run-now

"Run now" executes through the dashboard process's `Runtime`:

- It uses the same prerun/provider pipeline as `enso job run`. When that pipeline creates a run row, it records `trigger='manual'`; intentional prerun no-work creates no row.
- The provider runs in the job's named workspace under that workspace's configured policy. Invalid bindings fail closed before prerun or provider execution.
- Manual runs suppress Enso's automatic job failure/recovery notifications. A provider explicitly invoking `enso message send` remains an ordinary provider action and is not suppressed.
- The POST waits for the run to finish, then uses a `303` redirect to its run detail page when a run row exists. Intentional no-work redirects back to the job page with a status message. Live progress polling and output streaming are future work.

## Rendering & assets

- **Responsive layout**: the sidebar appears only when the viewport has room for it
  (1024px+). The Runs feed uses one record representation that changes from compact cards
  to a wide grid instead of duplicating hidden markup; the dashboard's short "Latest runs"
  list still keeps separate card and table markup. The capped main column stays
  left-aligned beside the sidebar on wide screens, and long IDs, paths, upload controls,
  and metadata must never widen the document.
- **Text editing**: Enso-owned `SKILL.md`, job prompts, shared and managed-root `AGENTS.md`,
  and reference docs use plain textareas; nested workspace instructions and
  external skills use escaped preformatted text. Rich Markdown rendering is not implemented.
- **Table grids**: schema and row values remain readable on narrow screens via
  bounded, horizontal overflow; long values cannot widen the whole document.
- **Form controls**: native single-select dropdowns share consistent spacing, focus
  states, and chevrons across browsers and themes.
- **Styling**: compiled Tailwind utilities are vendored as `web/static/tailwind.css`, with
  the small hand-written layer in `web/static/app.css`. Rebuild the generated file with
  `cd src/enso/web && npx tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.input.css -o static/tailwind.css --minify`. Light, dark, and system themes are user-selectable.
  Templates use semantic neutral tokens (`canvas`, `surface`, `border`, `ink`, `muted`,
  `action`, and related states) backed by CSS variables in `app.css`; green, amber, and red
  are reserved for success, warning, and destructive states rather than ordinary actions.
- **No external requests**: compiled CSS and image assets are vendored under `web/static/`,
  so the UI works offline and over a locked-down tailnet with no CDN trust or flash of
  unstyled content.
- **Navigation**: links, filters, pagination, and mutation forms use ordinary browser
  navigation. Successful mutations use `303` redirects to avoid resubmission on refresh.
- **Caching**: HTML remains `no-store` because pages contain a process CSRF token and local
  operational data.

## Non-goals (recap)

No chat, no login/accounts, no configuration or native-policy editor, no writes outside
Enso-owned paths under `~/.enso/`, no table-row/schema editor, no arbitrary SQL, no live
output streaming, and no public exposure. See [PRD.md](../PRD.md) § Non-goals.
