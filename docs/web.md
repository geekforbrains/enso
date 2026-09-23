# Web viewer

The optional web UI shows configuration, files, tasks, and execution history, and edits
instructions and secrets. There is no authentication: anyone who can reach it can read private
content and change what agents are told. Keep it on localhost or a private network such as
Tailscale. See [Access](#access).

## Running it

The [release installer](install.md#install) includes the `web` extra. Developers use the
[development launcher](development.md#local-development-loop).

```bash
enso web start                     # background, on 127.0.0.1:8787
enso web start --port 9000 --foreground
enso web install                   # optional user service
enso web status
enso web stop
enso web uninstall                 # stop and remove automatic startup
```

The viewer runs separately from `enso serve`, reading files and `enso.db` even when the
agent service is stopped. Browsing opens the database read-only. Writes respect maintenance
admission, and secret operations use short transactions. Without the optional dependencies,
`status`, `stop`, and `uninstall` still work; `start` and `install` explain the missing extra.

Standalone `start` logs to `~/.enso/web.log`. `install` adds a launchd/systemd user service
that logs to `~/.enso/launchd-web.log` and restarts after crashes and user logins.
[Install](install.md#the-viewer) owns service paths, environment capture, and adoption.
Once installed, `start` and `stop` control that supervisor. `stop` keeps it stopped until
the next start or user session; `uninstall` removes automatic startup. Neither affects the
agent service. `--foreground` runs until Ctrl-C.

For standalone or foreground starts, flags override `web.host` and `web.port`. Invalid or
missing configuration falls back to `127.0.0.1:8787` so Health can diagnose it. A supervised
viewer rejects bind flags: change config, then stop and start it.

The viewer holds `web.pid` locked while running. `start` and `stop` are idempotent, and
`status` exits 0 only for a running viewer. Pidfile operations reject symbolic links and
special files; `status` and `stop` do not create an absent file.

## Layout

Desktop uses a left sidebar with the current view marked. Views to watch and read come first,
then **Setup** (Workspaces, Jobs, Skills, Secrets); Health sits at the foot above the running
Enso version. There is no top bar. Pages show their name and render time, with no subtitle;
they do not refresh automatically.

| View | Sections |
| --- | --- |
| [Today](#today) `/today` | Schedule · Activity · Reliability |
| [Tasks](#tasks) `/tasks` | Project navigation, workflow, and grouped board |
| [Heartbeats](#heartbeats) `/heartbeats` | Current · Previous; each beat has Overview · History · Runs |
| [Runs](#runs) `/runs` | Acted · Failed · All |
| [Knowledge](#knowledge) `/knowledge` | Browse · All notes |
| [Workspaces](#workspaces) `/workspaces` | Home, then workspaces; each has Overview · Instructions, and workspaces add Files |
| [Jobs](#jobs) `/jobs` | Each job has Overview · History |
| [Skills](#skills) `/skills` | Skills grouped by scope |
| [Secrets](#secrets) `/secrets` | Add secret (default) · Secrets |
| [Health](#health) `/health` | Doctor · Log |

`/` redirects to `/today`. `/home` belongs to Workspaces. Filters sit in one row above
lists, with descriptive first options such as `Any job`. Search grows to fill the row;
`/` focuses it. In-place filters update the visible count. Server-filtered lists state
their scope beside the count. Empty lists show one sentence.

At 820px and below, navigation becomes **Today, Tasks, Heartbeats, Runs, More**. More contains
the remaining views in the same groups, highlights its active destination, and carries the
Health attention indicator. It uses native disclosure; JavaScript adds focus management,
Escape, and outside click dismissal. Section tabs become segmented controls. Filters wrap,
with equal-width selects above the search field. The layout respects phone safe areas and
works at 320px.

On iPhone, Share → Add to Home Screen installs Enso with its icon and opens Today without
Safari's address bar. Use the address you intend to keep accessing. Tap the current bottom
tab to reload. There is no service worker or offline mode.

The viewer uses server-rendered HTML, plain CSS, shared inline Lucide SVG icons, and one
progressive-enhancement script. No build step or network assets are required, and core
navigation works without JavaScript. Data-driven charts use SVG because the security policy
prohibits inline styles. CSS uses cascade layers, nesting, `light-dark()` tokens, container
queries, and cross-document transitions; transitions are disabled without scripting or with
reduced motion.

Rendered Markdown tables keep content-sized columns and wrap prose at word boundaries.
Tables wider than the reading area scroll horizontally within a focusable region; edge
shadows indicate more content, and the arrow keys scroll a focused table. Column alignment
from Markdown is preserved, including compact right-aligned numeric columns. This applies
to knowledge notes, workspace files, and other rendered Markdown, with or without JavaScript.
Task-list markers (`[ ]`, `[x]`, and `[X]`) at the start of list items render as disabled
checkboxes showing their saved state. The viewer cannot toggle them or modify the source;
links inside task text remain usable.

Preserve the compact layout and cloud/navy palette. Cyan marks links and selections; pink
is reserved for text selection. Status colours are consistent across themes: mint for
success, cyan for running, coral for errors, yellow for warnings/timeouts, and lavender for
muted states. Text and outlines need sufficient contrast; status dots also need an accessible
name and tooltip. This page owns these visual standards.

## Row standard

Lists share the `.row` grid, spacing tokens, and panel separators. Event slots are `when`,
`pin` (state), `body` (name and facts), and `trail` (value). Other row types adapt those slots:

| Type | Class | Shape | Trail |
| --- | --- | --- | --- |
| Event | `.row` | Time · state · name · facts · value | A measure, such as duration |
| Entity | `.row.entity` | State · name · facts · value | The primary sortable fact |
| Finding | `.row.finding` | Severity · message | None |
| Knowledge | `.row.knowledge-row` | Icon · name and location · value | Note age or folder count |
| Action | `.row.action-row` | Icon · name · action | Explicit control |

- The dot is the only row status indicator. Tags are for additional information, such as
  workspace finding counts; headings and detail pages may use status tags.
- Error messages belong on the destination page, except in finding rows.
- Supporting facts occupy one clipped line. Above 640px of list width, title and facts
  share a line with aligned title columns; below it, facts sit under the title.
- Use `.detail.columns` only when every row has the same facts in the same order. Missing
  facts use a dash. Jobs uses this above 920px; narrower lists let facts size to their text.
- A list uses one kind of trailing value, right-aligned with tabular figures. Bold identifies
  a primary value when a second line supports it; single-line values stay plain.
- Monospace identifies literals: paths, cron expressions, run IDs, and skill names.
- Navigation rows link to details. Never nest links or controls inside a linked row.

Phone rows retain time, state, name, and value, usually hiding supporting facts. Task rows
retain and wrap their reference, stage, phase, and origin; task timeline messages also wrap.
Lists must not require horizontal scrolling.

## Forms and actions

Reuse `static/app.css` tokens, `_macros.html`, and `_icons.html`. Use `.shead` for section
headings, `.right` for counts, `.note` for explanations, and `.range` for list counts.
Keep visible keyboard focus, explicit field labels, and associated help text.

When creation is the primary task, put it in the default tab and management in another,
using `subtabs` and ordinary links. Render only the selected view; do not repeat tab names
as headings. Put the saved total in the tab label, including zero, so it remains visible
from either view. A long list must not push the primary form below the fold.

Action rows are non-link containers with a leading item icon and trailing action form.
Use the Knowledge-style 16px inset, 12px gaps, a flexible wrapping name, and a fixed action
column. Keep the grid; event-row padding and a flex layout do not suit these controls.
Secret names remain monospace.

Use buttons for actions and links for navigation. Icon buttons use `.icon-button`, a 44px
target, and an accessible name and tooltip identifying the action and item. Delete uses the
trash icon and `.danger`; chevrons mean navigation or disclosure. Destructive row icons are
quiet at rest and coral on hover; explicit confirmation buttons use coral text and borders.

Destructive forms use `data-confirm` and native `window.confirm()` from `app.js`, naming
the item and consequence. Cancel sends no request. Attach the submit guard before revealing
`data-confirm-trigger` and hiding `data-confirm-fallback`. Without JavaScript, a disclosure
shows the warning, submit button, and Cancel link beneath the name; opening it never submits.
Keep the protected POST and redirect, with no inline handlers or custom modal.

Editors are plain forms: the file name with **Save** beside it, the path, and a labelled
monospace textarea. The form carries the revision it loaded. A save is refused if the file
has changed since, keeping the submitted text, showing the current file in a collapsed
disclosure, and adopting its revision so saving again deliberately replaces it. JavaScript
only warns before leaving unsaved text and adds Cmd/Ctrl+S. Saved text uses LF line breaks.

Successful writes redirect with a concise status message. Validation errors retain non-secret
input; secret fields always return empty. Verify action UI in a browser at desktop and phone
widths, with long names, keyboard focus, confirm/cancel, and JavaScript disabled. Request tests
alone do not verify layout or interaction. See [Browser checks](development.md#browser-checks).

## What it shows

### Today

**Schedule** opens with tiles for work in the last 24 hours, failures, the next check, and
heartbeats needing attention. Each links to its detail. Schedule owns the tiles and failure
banner; heartbeat attention remains visible in all three sections.

The schedule chart offers 6, 12, or 24 hours back, defaulting to 6, plus two hours ahead.
`/today?range=12` keeps the range across sections; unsupported values use 6. Its end is
rounded to the next hour plus two, so actual lookahead is two to three hours. The chart and
range control are hidden on phones.

Jobs get a lane when they have a run or scheduled slot in the window. Runs are positioned
by start time and duration, with a minimum visible width. A now line separates observed
runs from tinted future time. Future cron slots are ghosted, capped at 40 per lane; disabled
jobs and unscheduled stage jobs have none. **Up next** lists the nearest seven scheduled
jobs and active beat checks. Disabled Heartbeat contributes no upcoming checks.

Tiles, failures, and **Activity** always look back 24 hours from the current hour,
independent of chart range. Activity scans the newest 400 retained job and heartbeat runs,
groups them by hour, and folds consecutive `no_work` and `skipped` outcomes. Busy homes may
exceed that scan; [Runs](#runs) provides paginated history.

**Reliability** scans up to 400 job runs and uses the newest 30 per current job found in
that sample. Bars, counts, and average duration use the same sample; averages exclude absent
or zero durations. The sample can include older runs, contain fewer than 30 per job, or omit
a job entirely.

### Tasks

The [task board](tasks.md) at `/tasks` shows projects beside the board on desktop and above
it on phones. Project entries show name, key, workspace, and unfinished count, including
configured projects with no tasks. Tasks with missing project definitions remain reachable.
**All tasks** clears the project selection. Workspace selection narrows projects and tasks
and clears project/task filters.

Selecting a project shows its current workflow, with ordered stages, `done`, stage counts,
return destinations, and links to bound job instructions, including disabled jobs. Counts
cover the whole project, including completed history, regardless of task search; unavailable
counts are not shown as zero. Stage links filter the board. Project/stage links clear search,
and project links also clear the stage filter. Recorded execution evidence belongs to tasks.

| Group | Holds | Order |
| --- | --- | --- |
| Blocked | Human stages, blocked tasks, or attention flags | Oldest in stage first |
| Active | Claimed tasks | Newest claim first |
| Ready | Unclaimed tasks in executable stages | Project, then stage order |
| Backlog | Tasks not yet ready for a stage job | Oldest in stage first |
| Done | Finished and cancelled tasks | Newest first |

Each task appears once; empty groups are omitted. Filters `workspace`, `project`, `stage`,
and `q` (reference, title, body) apply before the 200-item completed-history cap. The count
line reports all matching tasks, any display limit, and completions in the last seven days.
Rows show reference, stage, origin, claim, current transaction phase, and time in stage.
A selected project omits repeated origin details.

At `/tasks/<ref>`, the task page shows its accepted stage, priority, claim, spec, refs,
handoff, timeline, and latest transaction notice. Submission, checking, repair, acceptance,
interruption, blocking, and operator overrides are distinct states. Submitting a handoff
or passing one check does not advance the displayed stage.

**Workflow history** retains each transaction's handoff, candidate revision, spec/workflow
versions, repair budget, timestamps, and run. Checks and lifecycle scripts show status,
exit code, duration, attempt, and literal diagnostics; lifecycle events retain retry IDs.
Missing evidence is never a pass, and provider success does not imply acceptance. Evidence
survives provider-run pruning without broken links. Manual checks are labelled **Operator
verification** and have no provider-run link.

The worktree panel uses the recorded path, branch, target, starting revision, and cleanup
status. A bounded Git query counts commits ahead of that target; unavailable counts say
unknown. Cleanup failures show their diagnostics. Completed cleanup retains ownership
history and omits the live count. Tasks without a worktree have no panel. All task operations
remain in chat and the CLI.

### Knowledge

`/knowledge` opens shared knowledge directly, with recently updated notes across all roots.
Retained workspace roots follow under **Workspaces**. [Knowledge](knowledge.md) owns note
format, links, writing, and imports.

**Browse** shows immediate folders alphabetically, then notes in the current folder.
**All notes** includes descendants; at Knowledge home it includes every root. Breadcrumbs
move up. Notes, folder context, and backlinks sort by updated date, then path; unknown update
dates use file modification time.

Search submits with Enter or **Search** and stays in the URL. Knowledge home searches every
root. Within a folder, search covers that folder and descendants; **All knowledge** broadens
it to every root. Notes rank by exact title/path,
literal title/path substring, matching path words, then a literal phrase in the body.
Path words can match in any order; typo tolerance applies only to alphabetic words of at
least four letters. Ties sort newest first, then by path. Browse search puts matching folders
first, alphabetically; All notes returns notes only.

Lists use `web.knowledge.page_size` (50 by default), counts, ranges, and Previous/Next links.
They never expand the entire tree. Rows show location and relative age through seven days,
then a local calendar date.

Notes with unique valid IDs open at `/knowledge/notes/<id>`, which survives moves. Other
notes use scope/path URLs and display metadata findings. Filenames supply titles; frontmatter
is hidden in the reading view. Dates appear above the body; **View source** shows the complete
file. **In this folder** opens a panel with up to 20 items and a link to the full folder.
It starts collapsed, giving the note the full reading area. With JavaScript and browser
storage available, the viewer remembers the choice across notes and reloads in that browser.
At 1000px and below, the panel opens above the note instead of beside it. The disclosure
also works without JavaScript, starting collapsed on each page. Paragraphs keep a comfortable
reading width while tables can use the full document width. **Linked from** shows up to 50
incoming notes and the total count.

Resolved wiki and Markdown links stay inside the viewer. Ambiguous or missing links are
marked; a missing heading is marked while its note remains clickable. Stable links, browser
history, and new tabs work without JavaScript.

Local PNG, JPEG, GIF, WebP, and AVIF images can display. Other attachments download; note
embeds become links. Remote images do not load automatically. Raw HTML is escaped, and only
`http`, `https`, and `mailto` external links are clickable. Attachments are limited to 20 MiB,
reject hidden/system paths and symlinks, and cannot escape their root. SVG and HTML download
as binary files, never as active same-origin documents.

Refresh discovers file changes and reuses unchanged parsed notes in memory. The viewer
creates no persistent index or missing directories and never changes notes or metadata.

### Workspaces

The list starts with **Home**, whose instructions, skills, and knowledge every workspace
shares, then workspace rows with their [audit](workspaces.md) verdict. Each page shows a
failing verdict in its heading and has these tabs:

| Tab | Home `/home` | Workspace `/workspaces/<name>` |
| --- | --- | --- |
| Overview | Path, workspace count, shared knowledge and skills, home audit findings | Projects, workflow previews, unfinished counts, task links, bindings, jobs, uploads, and audit findings |
| Instructions | `~/.enso/AGENTS.md` | The workspace's `AGENTS.md` |
| Files | None | Content roots and the file browser |

The file browser supports `work/`, `uploads/`, and retained `knowledge/` and `drafts/` roots;
absent optional roots are omitted. Knowledge cards open their scope in Knowledge. Workspace
files include dotfiles. Readable text and Markdown render in place; binary, unreadable, or
larger-than-2-MiB files show metadata. Markdown escapes raw HTML, leaves images as text, and
keeps only relative, `http`, `https`, and `mailto` links.

Workspaces and their parent container must be real directories. Browsing and summaries cannot
escape the permitted roots, including through symlinks; rejected paths return 404.

### Instructions

**Instructions** edits `AGENTS.md` in place; [Customizing](customizing.md#instructions-agentsmd)
owns what belongs there. Saving replaces the whole file atomically, keeping its permissions
and the `CLAUDE.md` link. A missing file is created on save with mode `0644`. Changes made on
disk or by an agent after the page loaded are never silently overwritten; see
[Forms and actions](#forms-and-actions). A symbolic-link, special, non-UTF-8, or
larger-than-128-KiB file is shown as an error without a form. A save over 128 KiB keeps the text
for trimming. Managed updates no longer refresh an edited home file; see
[the home-level file](customizing.md#the-home-level-file).

### Skills

Skills has one row per skill, grouped by workspace (`enso / <name>`), then home (`enso`),
then external `user` scope. Workspace groups are alphabetical. Rows show names and descriptions;
headings provide scope. Workspace and status filters narrow the list. Search and status update
the rendered page, counts, and empty headings without a reload; without JavaScript every row
in the selected workspace remains visible.

`/skills/<name>` shows every scope providing that name, making collisions visible together.
Entries include path, description, status, and reached workspaces. User-scope entries are
marked as outside Enso's management. [Customizing](customizing.md#skills) owns installation
and scope rules.

### Jobs

Job rows show directory name, schedule, workspace, executor, recent outcomes, and next run
or why it will not run. Facts align in equal columns on wide lists; fixed-width sparklines
align their newest run at the right edge. Stage jobs without cron show `when work is ready`.

**Overview** shows configuration, safely rendered prompt, gate/postrun commands and timeouts,
follow-up limit, concurrency policy, recent outcomes, and validation problems. Stage jobs also
show linked project/stage, project capacity, and required check names. Command and integration
executors show their type instead of a model. Hook script contents are not displayed.
**History** lists up to 500 job runs and links to paginated [Runs](#runs).

### Heartbeats

**Current** includes active and paused beats; **Previous** includes fulfilled, cancelled, and
expired beats until retention removes them. State filters narrow either view. Rows show
reference, title, workspace, timing, and attention state. Disabling Heartbeat keeps saved
records visible with a notice.

Each beat has **Overview**, **History**, and **Runs**. Overview shows instructions, completion
condition, allowed actions, saved agent/destination, timing, latest check, and progress.
History shows meaningful events with actors, decisions, action keys, and receipts; quiet
checks remain in the latest-check fields. Runs shows agent assessments, saved instructions,
input cutoff, outcome, and output.

Lists and events use 50-item pages and the [Runs](#runs) count/range rule. Long events are
visibly clipped with a CLI lookup for the full record. The viewer never edits or runs beats,
and reports incompatible databases without upgrading them.

### Runs

Runs are newest first:

| View | URL | Shows |
| --- | --- | --- |
| Acted | `/runs` | All except `no_work` and `skipped`; the default |
| Failed | `/runs?view=failed` | Errors, timeouts, gate failures, and interrupted heartbeat assessments |
| All | `/runs?view=all` | Retained history, with consecutive `no_work` and `skipped` outcomes folded |

The source filter selects jobs, heartbeats, or both. Job references are qualified, such as
`/jobs/team:digest` and `/runs?job=team:digest`. Job/status filters apply to the whole history;
an explicit status overrides the view. Pages contain up to 500 runs. Lists count first and
show the range actually read: a failed listing shows `0–0` beside its error, while a failed
count lists nothing. Lists never read run output. Beat checks without an agent appear only
in the beat's latest-check fields.

Run details show status, exit code, duration, trigger, executor/agent, session, diagnostics,
and retained output. Output renders as Markdown with line breaks preserved, falling back to
wrapped monospace above 256 KiB; errors always remain literal. Ordered attempts show each
execution and postrun result; attempt 0 is a reaction hook when no executor ran.
Stage runs include their **Task workflow** evidence and link to the task's complete history.
Provider completion and stage acceptance remain separate.

| Status | Meaning |
| --- | --- |
| `ok` | Executor and postrun checks succeeded |
| `error` | Executor, postrun check, or run failed |
| `timeout` | Execution exhausted its allowance and was stopped |
| `no_work` | Gate exited 1 or a stage job found no task; no executor ran |
| `gate_error` | Gate failed; no executor ran |
| `skipped` | Not admitted, such as a busy skip-policy group or expired wait |
| `running` | Waiting, executing, checking, or interrupted before closing |
| `cancelled` | Heartbeat assessment stopped before settlement |

Timeouts use yellow and errors coral; both count as failures. Final duration includes group
waiting and hooks, while execution timeout does not. [Jobs](jobs.md#postrun-scripts) owns
execution and postrun rules.

### Health

Health shows [`enso doctor`](cli.md)'s configuration, home/workspace, provider, transport,
service, job, Heartbeat, and knowledge findings. Problems come first; each section retains
its verdict, context, and expandable inspected details. Healthy sections say `No issues
found`. Knowledge checks validate structure rather than factual truth; Heartbeat checks
inspect state without executing gates or assessments.

It also shows the database footprint and schema readability, viewer version, home, bind
address, and capabilities. **Log** at `/health/log` shows the last 200 lines of `enso.log`.
Health remains available with invalid or missing configuration.

## Secrets

`/secrets` opens **Add secret**. **Secrets** at `/secrets?view=saved` lists saved names and
delete controls; its tab shows the total from either view. An instant case-insensitive name
filter shows matching counts; `/` focuses it. Without JavaScript all names remain visible.

Multiline values are normalized to LF. Use `enso secret add NAME --stdin` for exact bytes.
Saved values never appear in responses and cannot be revealed or edited. Duplicate creation
fails; replacement requires deleting the name and adding it again. Deletion uses native
confirmation, or an inline Delete/Cancel disclosure without JavaScript.

Secrets change only through `POST /secrets` and `POST /secrets/{name}/delete`. Creation
redirects to an empty Add secret form; deletion and Cancel return to the saved list. Success
shows **Secret added.** or **Secret deleted.** Errors stay in the relevant tab; the add form
retains the name but clears the value. Both actions use the [CLI](cli.md#secrets)'s store and
first-use key creation, without requiring the chat service.
[Configuration](configuration.md#secrets) owns key backup and restore.

## Access

The viewer binds `127.0.0.1` by default. It accepts `localhost`, address literals, its bind
host, and configured [`web.hosts`](configuration.md#web); other Host headers receive 421.
Routes declare their methods: missing routes return 404, unsupported methods 405, and browsing
uses GET only.

Writes require a same-origin URL-encoded form with an unguessable token. Cross-site requests
and mismatched Origin/Host are rejected. Safari's opaque Origin also requires its same-origin
fetch signal and token. Restart invalidates open forms; refresh before submitting. Write
workers retain maintenance admission even if the browser disconnects.

Every response carries security headers and a Content Security Policy restricted to the
viewer's own assets and safely served images. Pages and form responses use
`Cache-Control: no-store`; submitted secret values never appear in errors.

For phone access, use a private tunnel or authenticating reverse proxy and leave Enso on
localhost. Preserve the public Host header, add its name to `web.hosts`, and restart the
viewer. Binding elsewhere with `--host` does not add authentication. Write protection is
not a login system.
