# Web UI

The local dashboard: pages, routes, and read/write behaviour. Server-rendered, no chat.
See [architecture.md](architecture.md) for how the server runs and is secured, and
[data-model.md](data-model.md) for what it reads/writes.

## Shape

A small **server-rendered** app (Starlette + Jinja2), sprinkled with **HTMX** for inline
updates. There is no SPA, runtime build, or external CDN: compiled CSS and HTMX are
vendored under `web/static/`. Forms and links remain usable when JavaScript is off.

The whole UI is a thin skin over the file model and the runs DB: pages read `JOB.md` /
`SKILL.md` / `AGENTS.md` and the `runs` table, and writes go straight back to
those files (atomic replace) and to SQLite. There is no separate web database or cache —
the files are the model (see [data-model.md](data-model.md)).

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
| `/jobs/{name}/toggle` | POST | Implemented | Enable or disable a job |
| `/jobs/{name}/delete` | POST | Implemented | Delete a job directory after confirmation |
| `/jobs/{name}/run` | POST | Implemented | Run now and record a `manual` run |
| `/runs` | GET | Implemented | Run feed; filter by `?kind=`, `?name=`, `?status=` |
| `/runs/{id}` | GET | Implemented | Run metadata and captured log output |
| `/skills` | GET | Implemented | Enso-owned and external read-only skill tiers |
| `/skills/new` | GET, POST | **Planned** | Create an Enso-owned skill |
| `/skills/{name}` | GET | Implemented | View `SKILL.md`; edit controls appear for Enso-owned skills |
| `/skills/{name}/edit` | POST | Implemented | Replace an Enso-owned skill's `SKILL.md` |
| `/skills/{name}/delete` | POST | Implemented | Delete an Enso-owned skill directory after confirmation |
| `/agents` | GET | Implemented | View the working-directory `AGENTS.md` |
| `/agents/edit` | POST | Implemented | Save `AGENTS.md` atomically |
| `/static/*` | GET | Implemented | Vendored HTMX and CSS assets |

The toggle endpoint returns an updated HTMX fragment when requested that way; other
writes redirect to their resulting detail page.

## Pages

### Dashboard (`/`)

The dashboard shows:

- **Recent runs** — the last six rows from `runs`, newest first: kind, name,
  status pill (running/ok/error/timeout/prerun error/prerun timeout), trigger, duration,
  relative time; each links to `/runs/{id}`.
- **Jobs enabled** — the enabled and total job counts, linking to the job list.
- **Skills** — deduplicated Enso-owned and visible system counts, linking to the skill
  list.

### Jobs (`/jobs`, `/jobs/{name}`)

- Read: schedule, provider/model, timeout, notify destination, prompt body, and whether
  the configured prerun script exists.
- A dedicated **enable/disable** toggle flips `enabled:` for one-click pause, and
  **Run now** executes the job immediately.
- **Edit the prompt** (`/jobs/{name}/prompt`): save just the `JOB.md` body from the job
  detail page — the same edit-in-place affordance skills have (`/skills/{name}/edit`),
  preserving the original frontmatter text byte-for-byte.
- **Delete** (`/jobs/{name}/delete`): a native disclosure confirms the destructive
  action before removing the entire job directory, including prerun and companion files.
  Existing run history remains available.
- Recent runs for this job, linking to `/runs/{id}`.
- **Planned:** browser forms for create and full metadata/prerun editing. Until then use
  `enso job create` or edit the job files directly.

### Run detail (`/runs/{id}`)

- Metadata: kind, name/title, trigger, provider/model, status, exit code, start/end,
  duration.
- Output: up to the first 200,000 bytes of `runs/{id}.log`, monospace and wrapped. If
  truncated, the page shows the full byte count and on-disk path.
- A run with no captured output displays an empty-state message.

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
  (1024px+). Run history becomes readable cards below wide desktop sizes instead of
  relying on hidden horizontal scrolling, and long IDs, paths, upload controls, and
  metadata must never widen the document.
- **Text editing**: Enso-owned `SKILL.md`, job prompts, and `AGENTS.md` use plain
  textareas; read-only external skills use escaped preformatted text. Rich Markdown
  rendering is not implemented.
- **Styling**: compiled Tailwind utilities are vendored as `web/static/tailwind.css`, with
  the small hand-written layer in `web/static/app.css`. Rebuild the generated file with
  `cd src/enso/web && npx tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.input.css
  -o static/tailwind.css --minify`. Light, dark, and system themes are user-selectable.
- **No external requests**: compiled CSS and the pinned HTMX runtime are vendored under
  `web/static/`, so the UI works offline and over a locked-down tailnet with no CDN trust
  or flash of unstyled content.

## Non-goals (recap)

No chat, no login/accounts, no writes outside Enso-owned paths (`~/.enso/` plus the
working-directory `AGENTS.md`), no live output streaming, and no public exposure. See
[PRD.md](../PRD.md) § Non-goals.
