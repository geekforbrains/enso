# Web UI

The local dashboard: pages, routes, and read/write behaviour. Server-rendered, no chat.
See [architecture.md](architecture.md) for how the server runs and is secured,
[data-model.md](data-model.md) for what it reads/writes, and [tasks.md](tasks.md) for
task semantics.

## Shape

A small **server-rendered** app (Starlette + Jinja2), sprinkled with **HTMX** for inline
updates (change a status, add a tag, trigger a run, without a full reload). No SPA, no
build step, no external CDN — HTMX is vendored under `web/static/`. Every page works with
plain form posts if JS is off; HTMX is progressive enhancement.

The whole UI is a thin skin over the file model and the runs DB: pages read `TASK.md` /
`JOB.md` / `SKILL.md` / `AGENTS.md` and the `runs` table, and writes go straight back to
those files (atomic replace) and to SQLite. There is no separate web database or cache —
the files are the model (see [data-model.md](data-model.md)).

**Write boundary.** Every write the UI makes lands inside `~/.enso/` (tasks, jobs,
Enso-owned skills) or the working-dir `AGENTS.md`. It never writes outside that tree —
external "parent" skills discovered from the CLIs' own roots (e.g. `~/.claude/skills/`)
are strictly read-only. This is both the safety boundary and the ownership model: Enso
manages what lives in its own dir and only observes the rest.

## Routes

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Dashboard — recent runs, open-task counts, jobs at a glance |
| `/tasks` | GET | Task board/list; filter by `?status=` and `?tag=` |
| `/tasks/new` | GET, POST | Create-task form; POST writes a new `TASK.md` |
| `/tasks/{slug}` | GET | Task detail: description, status, tags, attachments, its runs |
| `/tasks/{slug}/edit` | POST | Update title/description/tags/notify/provider/model |
| `/tasks/{slug}/status` | POST | Move through the lifecycle (`todo`…`cancelled`) |
| `/tasks/{slug}/attachments` | POST | Upload a file into `attachments/` |
| `/tasks/{slug}/attachments/{name}` | GET, DELETE | Download / remove an attachment |
| `/tasks/{slug}/run` | POST | Run-now (records a `manual` run) |
| `/jobs` | GET | Job list — schedule, provider/model, enabled state |
| `/jobs/new` | GET, POST | Create-job form; POST scaffolds `~/.enso/jobs/<name>/JOB.md` |
| `/jobs/{name}` | GET | Job detail: frontmatter, prompt, prerun, recent runs |
| `/jobs/{name}/edit` | POST | Edit frontmatter (schedule/provider/model/timeout/notify), prompt body, prerun script |
| `/jobs/{name}/toggle` | POST | Enable/disable — edits `JOB.md` frontmatter |
| `/jobs/{name}/delete` | POST | Delete the job dir (confirmed; destructive) |
| `/jobs/{name}/run` | POST | Run-now (records a `manual` run) |
| `/runs` | GET | Global run feed; filter by `?kind=`, `?name=`, `?status=` |
| `/runs/{id}` | GET | One run: metadata + the captured `runs/{id}.log` body |
| `/skills` | GET | List skills in tiers: custom, bundled, external (read-only) |
| `/skills/new` | GET, POST | Create-skill form; POST scaffolds `~/.enso/skills/<name>/SKILL.md` |
| `/skills/{name}` | GET | Render a skill's `SKILL.md`; editable if under `~/.enso/skills/` |
| `/skills/{name}/edit` | POST | Edit an Enso-owned skill's `SKILL.md` (+ tool scripts) |
| `/skills/{name}/delete` | POST | Delete a **custom** skill (confirmed; destructive) |
| `/agents` | GET | Render `AGENTS.md` (the system prompt) |
| `/agents/edit` | POST | Save edits back to `AGENTS.md` in the working dir |
| `/static/*` | GET | Vendored assets (HTMX, CSS) |

`run` and `toggle` and `status` endpoints return an HTMX fragment (the updated card/row)
on success, or redirect back to the page for no-JS clients.

## Pages

### Dashboard (`/`)

Three at-a-glance panels:

- **Recent runs** — the last N rows from `runs` (all kinds), newest first: kind, name,
  status pill (running/ok/error/timeout/prerun error/prerun timeout), trigger, duration,
  relative time; each links to
  `/runs/{id}`.
- **Tasks** — counts per status (todo / in_progress / blocked), linking into `/tasks`
  filtered; a "New task" button.
- **Jobs** — each job with its next-fire time and last run status; disabled jobs dimmed.

### Tasks board (`/tasks`)

- Lanes or a list grouped by **status**; a tag filter (`?tag=`) narrows to a project.
  Because tags are freeform and double as projects, filtering by tag *is* the project view.
- Each card: title, tags, notify indicator, `updated` relative time, last-run status if
  any. Card actions (HTMX): change status, run now.
- "New task" → `/tasks/new`.

### Task detail (`/tasks/{slug}`)

- Rendered description (Markdown), status control, tag editor, notify toggle, optional
  provider/model override.
- **Attachments**: thumbnails/links for files in `attachments/`, an upload control, and
  per-file delete. Uploads are multipart, written into `attachments/` (filename
  sanitised; collisions suffixed).
- **Run now** — claims and runs this task immediately (see below), independent of the
  scheduler tick.
- **Runs** — this task's run history (`runs` where `kind='task' AND name=slug`), each
  linking to `/runs/{id}`.
- **Edit** — title/description/tags/notify/overrides, POST to `/tasks/{slug}/edit`,
  writing `TASK.md` atomically and bumping `updated`. The form carries the file's `updated`
  value; if it changed underneath (agent/CLI wrote concurrently) the save warns rather than
  silently clobbering — conflict *resolution* is out of scope (last-write-wins), but the
  operator is told.

### Create task (`/tasks/new`)

Form: title (required), description (Markdown textarea), tags (comma/chip input), notify
(checkbox), optional provider/model, and an initial attachment upload. POST scaffolds
`tasks/<slug>/TASK.md` with `status: todo` and any uploaded files — the same result as
`enso task create` followed by writing the body. The new task is immediately visible to
the task runner on its next tick.

### Jobs create/edit (`/jobs/new`, `/jobs/{name}`)

- Read: frontmatter (schedule, provider, model, timeout, notify), the prompt body, and the
  prerun script if present.
- **Full edit** (`/jobs/{name}/edit`): change the frontmatter, the prompt body, and the
  prerun script — atomic rewrite of the files under `~/.enso/jobs/{name}/`. A dedicated
  **enable/disable** toggle (`/jobs/{name}/toggle`) flips just `enabled:` for one-click
  pause, and **Run now** executes immediately.
- **Create** (`/jobs/new`): the same form with blank fields; POST scaffolds
  `~/.enso/jobs/<name>/JOB.md` (the `enso job create` result) and lands in the edit view.
- **Delete** (`/jobs/{name}/delete`): removes the job directory, behind a confirm
  (destructive).
- Recent runs for this job, linking to `/runs/{id}`.

### Run detail (`/runs/{id}`)

- Metadata: kind, name/title, trigger, provider/model, status, exit code, start/end,
  duration.
- Output: the `runs/{id}.log` body, lazy-loaded (it can be large; `output_bytes` gates a
  "load full output" affordance). Monospace, wrapped, downloadable.
- A `running` run shows a "still running / interrupted" banner rather than empty output.

### Skills (`/skills`, `/skills/new`, `/skills/{name}`)

Two tiers, split by the `~/.enso/` write boundary:

- **Enso skills** — everything under `~/.enso/skills/`, whether created here or seeded from
  Enso's starter set at install. Listed with name + description (from SKILL.md
  frontmatter), **fully editable and deletable**: `/skills/{name}` renders the `SKILL.md`
  and — being inside `~/.enso/` — offers edit (frontmatter + body + any tool scripts) and
  delete; `/skills/new` scaffolds a new one. Seeded starter skills are ordinary files here:
  Enso seeds them once and never re-syncs or resurrects them (depends on
  [issue #7](https://github.com/geekforbrains/enso/issues/7)); a "shipped with Enso" badge
  is cosmetic only.
- **External / "parent" skills** — auto-discovered from the underlying CLIs' own skill
  roots *outside* `~/.enso/` (e.g. `~/.claude/skills/`; the set of roots is configurable,
  see [data-model.md](data-model.md) § Config). Listed **read-only** with their absolute
  **source path** and owning CLI, so the operator sees everything available to the agent
  without the UI reaching outside `~/.enso/`. Detail renders read-only; no edit or delete.

### AGENTS.md (`/agents`)

- Renders the system prompt (`AGENTS.md` from the working dir).
- **Editable**: a textarea + save, POST to `/agents/edit`, atomic write back to
  `AGENTS.md`. The `CLAUDE.md` symlink to it is left intact (we write the target, not the
  link). This is the one system-prompt surface the operator can tweak without opening an
  editor.

## Run-now

"Run now" on a task or job is the web UI reaching into the **shared `Runtime`** (same
event loop, so a direct call — no IPC) to execute immediately:

- **Job run-now** → the same path `enso job run` uses, but recorded with `trigger='manual'`
  so it shows in history (the CLI `job run` today prints to stdout and records nothing; it
  gains run recording too).
- **Task run-now** → claim this specific task (`todo`/`blocked`/`in_progress` re-run
  allowed from the UI with a confirm), then run it through the task pipeline with
  `trigger='manual'`.
- The endpoint returns quickly with a "started" fragment; the run appears in the feed with
  `status='running'` and updates to its terminal status when done (HTMX poll or manual
  refresh — live streaming is a future idea, not v1).

## Rendering & assets

- **Markdown** (descriptions, SKILL.md, AGENTS.md) is rendered server-side. Enso already
  converts Markdown for Telegram/Slack (`formatting.py`); the web renderer is a separate,
  HTML-targeted path (a small dependency or a minimal renderer — decided at build time,
  kept out of the base install).
- **Styling**: one hand-written stylesheet under `web/static/`; no framework. Dark-mode by
  default is nice-to-have.
- **No external requests**: all CSS/JS is vendored, so the UI works offline and over a
  locked-down tailnet, and there is no CDN to trust.

## Non-goals (recap)

No chat, no login/accounts, no writing outside `~/.enso/` (external "parent" skills are
read-only), no live output streaming (v1), no public exposure. See
[PRD.md](../PRD.md) § Non-goals.
