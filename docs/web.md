# Web viewer

An optional, read-only web UI. It answers the questions you cannot answer from a chat
window: what can this agent actually see, what is it configured to do, and what happened
last time it ran.

**Read-only is a design decision, not a phase.** The viewer serves `GET` and nothing else.
It cannot start a job, edit a file, change config, or send a message. It has no authentication,
so keep it on localhost or behind authenticated private access: read-only pages still expose
private prompts, files, and run output. See [Access](#access).

## Running it

```bash
# From the Enso source checkout; keep the transport extras you use.
uv tool install -e '.[web]'          # or install with [slack,telegram,web]

enso web start                     # background, on 127.0.0.1:8787
enso web start --port 9000 --foreground
enso web install                   # optional user service, starts again after login/reboot
enso web status
enso web stop
enso web uninstall                 # stop and remove automatic startup
```

The `web` extra brings `aiohttp`, `jinja2`, and `markdown-it-py`. Without it, `status`,
`stop`, and `uninstall` still work; `start` and `install` print the install command
instead of a traceback.

It is a separate process from `enso serve`, deliberately: the viewer can be restarted,
crashed, or left uninstalled without touching the bridge. It reads `enso.db` and the files
under `ENSO_HOME` directly, so it shows the truth even when the service is stopped.

Without an installed viewer service, `start` runs the viewer in the background in its
own session with its output in `~/.enso/web.log`, and prints the URL once it answers.
That process lasts until shutdown. `install` adds an optional launchd/systemd user
service, with output in `~/.enso/launchd-web.log`, that restarts after crashes and at
future user logins. See [Install](install.md#the-viewer) for unit paths, reboot/login
requirements, environment capture, and adoption of existing units.

Once installed for this home, ordinary `start` and `stop` control the viewer's supervisor.
`stop` keeps it stopped until the next start or user session; `uninstall` also removes
automatic startup. Both leave the agent service alone. For a supervised viewer, change
`web.host` or `web.port` in config, then run `stop` and `start`; command-line bind overrides
are rejected. `--foreground` runs the same viewer directly in your terminal until Ctrl-C,
and is also the unit's entrypoint. The viewer holds a lock on
`~/.enso/web.pid` for as long as it lives, which is how `status` tells a live viewer from
a stale file and how `stop` knows the pid it signals is really the viewer. `start` is a
no-op while the same viewer is running, `stop` is a no-op while it is not, and `status`
exits 0 only while it runs, so all three are safe in scripts.

Every command that opens `web.pid` refuses symbolic links and special files, including
named pipes, with an error instead of following the link or waiting for the pipe.
`status` and `stop` leave an absent pidfile absent.

For standalone and foreground starts, `--host` and `--port` win over `web.host` and
`web.port` in `config.json`. A missing or invalid `config.json` does not stop the viewer:
it falls back to `127.0.0.1:8787` (flags
still win) so the Health page can show you the problem. It writes nothing but the pidfile
and its log, and it opens `enso.db` read-only, so nothing a page does can change Enso's
state or block the service.

Server-rendered HTML, one stylesheet, one small script for filtering and sorting that the
pages work without, no build step, no framework, and nothing loaded from the network. The
Content-Security-Policy allows no inline styles either, so anything whose shape depends on
data — the schedule chart, the run bars, the timeout meter — is an inline SVG.

The stylesheet is plain modern CSS: cascade layers in place of specificity games, native
nesting, `light-dark()` colour tokens so there is one palette rather than a light and a dark
copy, container queries so a list adapts to the space it is given rather than the viewport,
and a cross-document view transition so moving between pages does not flash. Cloud and navy
surfaces carry Enso's palette without changing the compact layout. Cyan marks current
selections and links; pink appears only in text selection. Icons are inline SVG drawn from
the Lucide set, so they share one stroke weight and take the text colour.

Status meanings stay the same across light and dark themes: mint for healthy or successful,
cyan for running, coral for errors, sun yellow for warnings and timeouts, and lavender for
muted states. Dots, chart marks, and legends share the same fills. Deeper shades keep text
and outlines readable on light surfaces; bright status lamps use navy text. Status-only
dots expose their existing state as an accessible name and a tooltip, so their meaning is
available without distinguishing the colours.

This page owns the viewer's visual language; marketing typography, taglines, and extra
decoration do not replace the viewer's familiar density or interaction patterns.

## Layout

Desktop navigation is a left sidebar and nothing else: there is no top bar, and the current
view is marked by a bar on its left edge. Each page opens with its name and the time it was
rendered, because pages do not refresh themselves, and no page carries a subtitle; a fact
worth knowing lives in a definition list or on the line that counts the rows. Each view keeps
its named sections above the content:

| View | Sections |
| --- | --- |
| [Today](#today) `/today` | Schedule · Activity · Reliability |
| [Tasks](#tasks) `/tasks` | project navigation, selected workflow, and one board grouped Blocked · Active · Ready · Backlog · Done |
| [Heartbeats](#heartbeats) `/heartbeats` | Current · Previous; each beat opens Overview · History · Runs |
| [Jobs](#jobs) `/jobs` | a job opens to Overview · History |
| [Runs](#runs) `/runs` | Acted · Failed · All |
| [Knowledge](#knowledge) `/knowledge` | Folders · All notes |
| [Memory](#memory) `/memory` | Memories · Captures |
| [Workspaces](#workspaces) `/workspaces` | Workspaces · Skills |
| [Health](#health) `/health` | Doctor · Log |

`/` redirects to `/today`. Skills are resolved per workspace, so `/skills` is a section of
Workspaces and highlights it.

Filters are one row of controls at the top of a list. A control's first option says what it
filters (`Any job`, `Any status`), so nothing sits above it; the search field grows to fill
the row and `/` focuses it from anywhere on the page. A list that filters in place shows a
live count at the end of the row; a list that filters on the server says what the view
includes on the line that counts its rows. An empty list is one quiet sentence where the
rows would have been.

At 820px and below, the sidebar becomes a bottom bar with exactly five items: **Today,
Tasks, Heartbeats, Runs, More**. More opens a compact popup containing Knowledge, Memory, Jobs,
Workspaces, and Health. It stays highlighted while one of those views is open, and carries the Health
attention indicator even while the popup is closed. The bar respects the phone's safe area
and stays within the viewport at 320px wide.

On an iPhone, Share → Add to Home Screen installs the viewer as an app named Enso, with the
Enso logo as its icon. It opens full screen on Today, without Safari's address bar, so the
bottom bar is the whole navigation, and the status bar takes the page's own light or dark
ground. The files behind that are a web app manifest and the icons under `/static/`, read
once at install time; there is no service worker and nothing works offline, because a viewer
that shows the truth must not show a cached copy of it. Pages still do not refresh
themselves, and there is no address bar to reload from: tap the current tab in the bottom
bar, which loads the page again. The app opens the address it was installed from, so install
it from the address you will keep using (see [Access](#access)).

More uses native `<details>` and `<summary>`, so every destination remains reachable without
JavaScript. With JavaScript, opening moves focus into the popup, Escape closes it and returns
focus, and clicking or moving focus outside closes it. The section strip becomes a segmented
control on phones; wide-only content such as the schedule chart is hidden below 820px rather
than squeezed. Quiet polls still fold with native `<details>`. A filter toolbar wraps there
too: its dropdowns share their row in equal widths rather than sizing to their labels, and
the search field takes the row below.

## Row standard

Every list in the viewer is built from one CSS grid with four slots, in this order:

| Slot | Holds |
| --- | --- |
| `when` | a time, on rows that describe a moment |
| `pin` | the status dot |
| `body` | a title and one line of supporting facts |
| `trail` | the right-hand value |

There are three row types, and a list picks one:

| Type | Class | Shape | Trail holds |
| --- | --- | --- | --- |
| event | `.row` | when · state · name · facts · value | a measure, such as a duration |
| entity | `.row.entity` | state · name · facts · value | the one fact you would sort the list by |
| finding | `.row.finding` | severity · message | nothing |

The rules that keep them consistent:

- **The dot is the row's whole state, and the only place it appears.** No status tag repeats
  what the dot already says. In a list row a tag survives only where it carries something a dot
  cannot — today that is one place, the finding count on Workspaces. Headings and detail pages
  still use tags freely; the rule is about rows.
- **A row never carries an error message.** It says that something is wrong; the page behind it
  says what. A finding is the exception, because there the message *is* the row.
- **The detail line is one line** of stable facts you would scan — a schedule, a workspace, a
  scope. Nothing whose height varies with the data, which is what made failing rows the tallest
  and ugliest ones on a page; a single long fact such as a skill's description is fine, because
  it is clipped to the one line. Given room (a list wider than 640px, measured on the list itself,
  not the window) the title and the facts share one line, the title in a fixed-width column so
  the facts line up down the list like columns; narrower, the facts drop under the title.
- **A detail line whose facts are the same in every row can ask to be columns.** Facts otherwise
  size to their own text, so one row's `0 10 * * *` starts its workspace where the row above it,
  reading `when work is ready`, has already reached its agent. `.detail.columns` splits the width
  evenly instead and gives each fact one axis the whole way down, which also fixes the row's chart
  and trail columns so that every row is dividing the same box. It is opt-in because most detail
  lines vary in what they hold, and it holds only while every row of that list spends the same
  slots in the same order, so a row that cannot answer one dashes it rather than dropping it.
  Today that is Jobs. It needs its own breakpoint, a list wider than 920px rather than 640px,
  because a third of the width has to hold the widest of the three facts; narrower than that the
  facts go back to sizing themselves, which spends the width where the text actually is.
- **The trail holds one kind of value per list**, the same kind in every row of that list, right
  aligned with tabular figures. Bold marks the primary value and a second line supports it; a
  single line takes no bold.
- **Monospace marks a literal**: a cron expression, a path, a run id, a skill name. A row's
  title is otherwise plain, so a column of names scans as names.
- **Every row has somewhere to go.** A row that omits detail must link to a page that holds it.
  Stripping a row without giving it a destination does not simplify the information, it deletes
  it: that is why the skills list has a skill page behind it.
- **Nothing inside a row is a link.** The row is the link. An `<a>` inside an `<a>` makes the
  parser close the outer one and reparent the rest, which silently takes the row apart; a test
  walks every page counting anchor depth to keep it that way.

On a phone the same row keeps its time, dot, name and trailing value and usually drops the
detail line. Task details wrap to retain their reference, stage, current phase, and
project/workspace origin; the claim's run ID is on the task page. Task event messages also
wrap and stay because they are the substance of the timeline. Lists never need horizontal scrolling.

## What it shows

### Today

The landing page, and the one that answers "did anything break while I was asleep".
Schedule opens with four tiles: runs that did work in the last 24 hours with the gated count
beneath, failures, the next scheduled check, and heartbeats that need your attention. Each
links to the page that explains it. On a phone, where the chart is hidden, the tiles are the
page. The tiles and the failure banner under them belong to Schedule alone: Activity and
Reliability are the detail behind those numbers, so they open straight onto their own list.
Heartbeats needing attention stays on all three.

**Schedule** offers 6, 12, or 24 hours back, defaulting to 6, with two hours ahead.
The range lives in the URL (`/today?range=12`) and follows the Schedule, Activity, and
Reliability section links; an unsupported value falls back to 6. Both the chart and
its range control are hidden on phones.

The chart's right edge is pinned to the next top of hour plus two hours, so the real
lookahead stays between two and three hours. The left edge floats between five and six
hours back at the default range, and the columns remain whole hours. Labels appear every
hour at 6, every two hours at 12, and every three hours at 24.

A current job gets a lane only when it has a run event in that chart window or a scheduled
slot within it. Each run is placed at the time it started and drawn as wide as it took, with
a minimum width to keep short runs visible and a line marking now. Everything right of that
line is scheduled rather than observed, and its ground is tinted to say so; the chart draws
one hairline per hour and nothing per cell. Future cron slots are ghosted across the forward
strip, capped at the first 40 per lane; disabled jobs and stage jobs without a cron schedule
have no ghosts. Below it, Up next combines the next checks for active beats with scheduled
jobs, showing the nearest seven. Disabled Heartbeat adds no upcoming work. Beats needing
attention are linked separately.

The summary tiles, failure banner, and **Activity** retain their 24-hour lookback from the
start of the current hour at every chart range, so zooming in cannot hide an overnight
failure. Activity shows runs from the newest 400 retained job and heartbeat runs,
newest first and grouped by hour. Consecutive `no_work` and `skipped` outcomes fold into
one line you can open. Busy homes may have more runs in the time window than this page
scans; use [Runs](#runs) for the paginated history.

**Reliability** uses a separate scan of up to 400 job runs, then takes up to the newest
30 for each current job represented in the scan, including runs older than the schedule
window. Duration bars, run and failure counts, and average durations all use that same
per-job sample; averages include only runs with a recorded, nonzero duration. A busy home
may leave fewer than 30 runs for a job in the scan, or none, in which case the job is absent.

### Tasks

The [board](tasks.md) stays on one page at `/tasks`. Project navigation sits beside it on
desktop and above it on phones, where the project list scrolls within a compact area. Each
project shows its name, key, workspace, and unfinished task count. Configured projects remain
visible with no tasks; tasks whose project definition is missing remain reachable too. **All
tasks** returns to the board across projects. A workspace filter narrows both the project
list and board; changing workspace clears the project and task filters.

Selecting a project shows a compact workflow preview above its tasks. Open it for numbered
stages in order, followed by `done`; choosing a stage keeps the details open. Each step shows
its execution kind (Agent, Human, Command, or Integration), required check names, an allowed
return destination, and the number of tasks at its accepted stage. Agent stages have a
**View instructions** link to each bound job's prompt, including disabled jobs. Clicking
the stage itself filters the board to that stage. Project and stage links clear task
search; project links also clear the stage filter. Workflow counts describe the whole project,
independent of task search, and include all completed history without loading task bodies.
Backlog, blocked, and cancelled tasks remain in the board outside the forward workflow.
Missing counts are shown as unavailable, not zero. The project's workspace links to its detail
page. This is a view of the current configuration; recorded execution evidence belongs to
each task's Workflow history.

The board puts work needing attention first, then work the agents hold. Every task is in one
of five groups, in this order, each headed by its name and its count:

| Group | Holds | Order |
| --- | --- | --- |
| Blocked | tasks in a human stage, blocked, or flagged for attention; the heading says they need you | oldest in stage first, so the one that has waited longest is on top |
| Active | claimed tasks; the task page links to the run that holds it | newest claim first |
| Ready | unclaimed tasks in agent stages | pipeline order: by project, then the project's own stage order |
| Backlog | backlog tasks, which are not ready for a stage job to claim | oldest in stage first |
| Done | finished and cancelled tasks | newest first |

A task's state puts it in exactly one group, so nothing is listed twice, and a group with
nothing in it is left out rather than shown empty.

The line above the board counts every matching task, says how many of them are listed when
the cap bites, gives the number completed in the last seven days, and states the limit of
200 finished and cancelled tasks. Filters apply before the cap, and finished history is
counted without loading every task into the page.

`workspace`, `project`, `stage`, and `q` (matched against the reference, title, and body) narrow
the whole board, groups and count line together, and an emptied board names the filter that
emptied it. Workspace filtering also applies to completed history before its limit.
Rows follow the [entity row standard](#row-standard): the dot is the state (needs you,
blocked, active, ready, done, cancelled), the title is the name, the detail line is the
reference, stage, project and workspace, and who holds it; a selected project omits its repeated
origin from each row. The trail is the time in its stage. While a transaction is active, the
detail line also names its phase: working, handoff submitted, running required checks, or
repairing failed checks. The row is the link. The board reads current transaction summaries in one bulk
query; it does not load every task's check output or timeline.

A task's own page at `/tasks/<ref>` is its record: the heading carries the reference, the
title, and the attention flag when it is set. A workflow notice states the latest transaction
outcome and links to its evidence. The breadcrumb and project name return to the project's
Tasks view, and its workspace links to the workspace detail page. The panel shows the stage
as the project's pipeline with the current one marked, the priority, and the claim. The pipeline
shows the **accepted stage**: submitting a handoff or passing one check does not advance it.
The notice distinguishes work in progress, submission, checking, repair, acceptance,
interruption, and blocking. Recorded operator overrides are labelled separately from success.

**Workflow history** is an expandable record of each transaction, newest first. The summary
names the stage, destination, and Enso's recorded acceptance status. Open it for the submitted
handoff, candidate revision, spec and workflow versions, repair budget used, timestamps, and
the linked run. Required checks and lifecycle scripts each show their recorded status, exit
code, duration, attempt, and captured diagnostic/output; lifecycle scripts also identify the
event for retry auditing. Script output stays literal and escaped. No check result is inferred
from an agent's prose or a successful provider exit, and absence of evidence is never labelled
as a pass. Workflow evidence remains available when retention has pruned the provider run;
the old run is named without a broken link. Manual check execution is labelled
**Operator verification** in claims, workflow evidence, and timeline entries, with no
provider-run link because it never created a provider run.

The worktree panel uses the task's recorded path, branch, target branch, starting revision,
and retention or cleanup status. It works for locations outside the Enso home and does not
infer the target from whatever branch the main checkout happens to have open. A failed
cleanup stays visible with its diagnostic. A bounded read-only Git query counts commits
ahead of the recorded target; if Git or the worktree is unavailable, the metadata still
renders and the count says unknown. After recorded cleanup, the panel preserves ownership
history and omits the live commit count. No worktree record means no panel.

The rest of the page holds the spec rendered with the same safe Markdown renderer as
workspace files, refs, accepted handoff, and timeline. Timeline rows show the actor, message,
and surviving run link. Which moves are available is the agent's business and is not shown;
the viewer moves nothing, because that is chat and the CLI. See [Tasks](tasks.md) for the
workflow and worktree contracts.

### Knowledge

`/knowledge` reads the home's `shared/knowledge/` and automatically discovers every visible
workspace `knowledge/` directory. **Folders** starts at the Knowledge home, where **Shared**
and each workspace are folders to enter. The home first shows the most recently
updated notes across every root, up to `web.knowledge.recent_limit` (5 by default), then the
root folders under **Workspaces**. Its breadcrumb is **Index**. The Markdown files remain the
source of truth. See [Knowledge](knowledge.md) for metadata, writing conventions, imports,
and the agent's maintenance tools.

Away from the home, **Folders** shows immediate subfolders followed by notes directly in
the current folder, including folders that have both. Breadcrumbs move up the hierarchy.
**All notes** lists the current folder and all its descendants, newest updated first; it uses
`updated` metadata and falls back to file modification time when that date is unknown. Search
matches titles, paths, and note bodies under the current folder. The explicit **All knowledge**
search option broadens to every discovered root. Every list shows at most
`web.knowledge.page_size` items per page (50 by default), with its count, range, and ordinary
Previous/Next links. Thousands of notes never produce a fully expanded tree or an unbounded
page. Note rows show seconds, minutes, hours, or days ago through seven days, then a local
calendar date such as `Jan 1st, 2025`.

A note opens at `/knowledge/notes/<id>`, using its permanent ID so the URL survives moves
and renames. Notes without a valid or unique ID remain readable through an explicit
scope-and-path URL and show their metadata findings. The title comes from the filename;
frontmatter is omitted from the reading view. Created and updated dates appear above the
body, and **View source** shows the complete file. The note's folder appears beside it on a
wide screen and below it on a narrow one. **Linked from** shows up to 50 incoming notes and
the total backlink count. Folder context is capped at 20 items, with a link to the complete
paginated folder.

Wiki links (`[[Page]]`, `[[Folder/Page|Label]]`, `[[Page#Heading]]`) and ordinary relative
Markdown links navigate inside the viewer. Cross-scope links explicitly name `shared:`
or `workspace:<name>:`. Duplicate filenames require a qualified path: ambiguous and missing
references are visibly marked instead of selecting a destination arbitrarily. Missing
heading anchors are marked while the destination note remains clickable. Headings get
Unicode-aware anchors with `-1`, `-2` suffixes for repeats. Browser back/forward and opening
a link in a new tab work normally; no JavaScript is required.

Local Markdown images and Obsidian image embeds display PNG, JPEG, GIF, WebP, and AVIF
assets under the selected knowledge root. Other attachments download; note embeds become
links to their notes. Remote images never load automatically. Raw HTML is escaped and
only `http`, `https`, and `mailto` external links are clickable. Asset reads are bounded at
20 MiB, reject symlinks and hidden/system paths, and never follow a path outside its root.
SVG and HTML attachments download as binary files, never as active same-origin documents.

Each visit scans current file metadata and reuses unchanged parsed notes in process memory;
refreshing sees additions, moves, and edits. The viewer builds no persistent index, creates
no missing directories, and never changes Markdown, metadata, or the database. Knowledge
appears in the desktop sidebar and the phone's **More** menu.

### Memory

`/memory` audits one workspace at a time. The header counts its captures, captures without
a processing receipt, current Markdown memories, and whether its `enso-memory` job is installed
and enabled. Missing or invalid jobs are shown without hiding the stored records. The
**Memories** and **Captures** tabs share the workspace selection; neither writes state.

**Memories** searches titles, paths, and bodies and lists at most 50 notes per page, newest
occurrence first. Each row shows a status dot, readable note name, source count, and time
since occurrence (or **Undated**). Names replace filename hyphens and underscores with spaces,
omit a trailing UUID, and capitalize the first letter. The same name appears on the detail
and linked capture pages; title search accepts this readable form as well as the original path.
A detail renders the Markdown without active HTML or images and shows
the occurrence, creation and update dates, exact path, metadata findings, and source
capture links. A manual memory with no sources says so. A missing source remains labelled
as missing. Links to other current memories remain within their workspace; unresolved
links are inert.

**Captures** filters Slack or Telegram and shows at most 50 stored messages per page,
newest stored first. A detail shows the escaped stored text, truncation flag, sender,
original time, conversation and thread, attachment references, kind, handling and delivery
outcomes, linked parent or reply, nearby stored context from the same conversation, and
current memories citing it. The processing label comes from the durable receipt:
unprocessed, publication pending, processed with memory, or processed with no memory.
A completed receipt remains processed even if its memory file is later removed. The page
does not backfill chat history; live capture is best effort, text can be truncated, and
attachments are references rather than extracted content. See [Memory](memory.md) for the
capture and processing contracts.

### Workspaces

The list is a workspace name and its [audit](workspaces.md) verdict, so a malformed workspace
is visible before it surprises you. Its own page starts with its projects: unfinished task
counts and an ordered workflow preview, with human stages marked. Each project links back
to its workspace-filtered Tasks view; **All tasks** opens all work for that workspace.
The page also shows what is bound to it, the jobs that name it, its upload size, and every
audit finding — which required directories exist, whether `CLAUDE.md` and the skill links
are correct, whether any skill name collides.

A workspace and its parent container must be real directories, matching CLI ownership;
linked workspaces return 404. Directory summaries do not scan roots that escape the workspace.
A workspace's `knowledge/` card opens its scope in [Knowledge](#knowledge). The existing
file browser is available over `knowledge/`, `work/`, `drafts/`, and `uploads/`, with text and
Markdown rendered in place. This is the "what files is the agent reading" view: it shows what is actually on
disk in the directories the agent has been told to use, dotfiles included. Files over 2 MiB,
binary files, and anything unreadable show their metadata instead of a body. Markdown is
rendered with raw HTML escaped, images left as text, and only relative, `http`, `https`,
and `mailto` links kept, so a file the agent wrote cannot make your browser fetch or run
anything. Absent optional content roots are omitted from the workspace page. The browser never
leaves these allowed directories: a path that resolves outside them, including through a
symlink, is a 404.

### Skills

One row per skill — not one per skill per workspace, which produced hundreds of near-identical
rows — carrying the name and its description, filterable by workspace and status.

The list is grouped by where a skill comes from, under the same headings the Tasks board uses:
one group per workspace holding skills of its own, headed `enso / <workspace>` because a
workspace is a directory inside the Enso home, ordered alphabetically by workspace name, then
`enso` for the home's own skills, then `user`. The `enso /` part is drawn quieter than the
workspace name, and nothing outside these headings takes the prefix. A group with nothing in
it has no heading, and the
scope is not repeated on the row because the heading above it already says so. The count line
above the list carries the total and how many are in error or warning. The search field and the
status filter narrow the rendered page rather than reloading it, so a heading disappears with
its last visible row and its count follows what is left below it; with JavaScript off every
group and row is present and the counts are the page's own.

A skill's own page at `/skills/<name>` lists every scope that provides that name, which is
exactly the shape of a collision, so it reads in one place. `/skills/research`:

| Scope | Path | Status |
| --- | --- | --- |
| workspace | `workspaces/blog/skills/research` | active |
| enso | `~/.enso/skills/research` | collides with the workspace scope (error) |

Each entry carries its path, its description — so you can see what the agent thinks it is for —
and the workspaces it reaches. A user-scope entry is marked as not managed by Enso.

### Jobs

The list is a job's directory name, its schedule, workspace and agent triple, a sparkline of
recent outcomes, and when it runs next — or why it does not. The three facts are
[equal columns](#row-standard) on a wide list. The sparkline is a fixed-width box the marks never
scale inside, so it is anchored at its right edge: the newest run sits on the same axis in every
row, beside the next-run value, and a short history trails off to the left instead of floating
away from it. A [stage job](jobs.md#stage-jobs) without a cron line shows `when work is ready` in
place of a schedule, here and on Today. Its
own page adds Overview and History: the configuration and the prompt body, rendered with the
same safe Markdown renderer as workspace files (for a stage job, the project and stage it
serves, linked to the board filtered to that project and stage, and its effective concurrency
group), prerun and postrun filenames and timeouts, the follow-up limit, a chart of recent
outcomes, project capacity and configured stage check names, and any `JOB.md` problems keeping it from running. Command/integration stages are labelled as Enso engine execution with no model. It does not display hook script
contents. History shows up to 500 runs of the job; the link to the filtered Runs page provides pagination when
retention is higher.

### Heartbeats

The [Heartbeat](heartbeat.md) page at `/heartbeats` shows current beats, including paused
ones. Previous shows fulfilled, cancelled, and expired beats until retention removes them.
The state filter narrows either view. Rows show the title, reference, workspace, and timing;
the status dot also identifies beats that need attention. When Heartbeat is disabled, its
saved records remain visible with a clear notice.

Each beat opens to Overview, History, and Runs. Overview shows the instructions, completion
condition, allowed actions, saved agent and destination, timing, latest check, and current
progress. History pages through meaningful events with their dates, actors, decisions,
action keys, and receipts. Each event is a row that opens; a row names the actor by its
origin, so a chat identity such as `slack:U0AETSSDDEF` reads as the platform a person used
and the recorded value stays in the opened event. Quiet checks stay out of the timeline.
Runs links to each agent assessment, including its saved instructions, input cutoff,
outcome, and output.
Lists and history use pages of 50 under the [Runs](#runs) range rule. Long event content is visibly clipped with a CLI lookup
for the full record; opening a run shows its retained provider output.

The viewer cannot pause, edit, fulfill, or otherwise run a beat; those changes happen through
the agent and CLI. Viewing an incompatible database reports its error without upgrading it.

### Runs

Run history, newest first. Three views:

| View | URL | Shows |
| --- | --- | --- |
| Acted | `/runs` | everything except `no_work` and `skipped` outcomes; the default |
| Failed | `/runs?view=failed` | errors, timeouts, prerun failures, and interrupted heartbeat assessments |
| All | `/runs?view=all` | the whole retained history, consecutive `no_work` and `skipped` outcomes folded |

Job links and filters use qualified references: `/jobs/team:digest` and
`/runs?job=team:digest` (the colon may be URL-encoded as `%3A`).

The source filter selects all work, jobs, or heartbeats. `job` and `status` narrow any view
and still apply to the whole history; an explicit `status` overrides the view. `page` pages
at 500 (the default job retention). Every paginated list counts first and then lists one
page: the shown range comes from the rows actually listed, so a failed listing reads `0–0`
of the counted total beside its error, and a failed count shows its error and lists nothing.
The list never reads a run's output. Heartbeat checks that did not involve an agent are
absent; the beat's Overview holds its latest check.

Each job run opens to its full record: status, exit code, duration,
trigger, the agent that ran it, the session ID, final postrun diagnostic, the error, and the
latest provider output, which can be the whole retained megabyte. Output renders as Markdown
with its line breaks kept, so a transcript's own fences and tables read as themselves, and
falls back to wrapped monospace above 256 KiB. Errors and postrun errors stay monospaced
always, because their exact spacing is the information. A heartbeat run's output renders
the same way, being the same kind of provider transcript. Ordered attempts show each provider
turn and its postrun result and feedback; attempt 0 represents a reaction hook when no
provider ran. Older history has no attempts.
For stage jobs, **Task workflow** shows the transactions belonging to this run using the
same evidence view as the task page, with a link back to the task's complete audit history.
Provider completion and stage acceptance are separate facts; the recorded transaction
status states whether the handoff was accepted.
The run remains `running` while postrun validates or requests a follow-up. Final duration
includes hook time; the provider timeout allowance does not. See [Jobs](jobs.md#postrun-scripts).

| Status | Meaning |
| --- | --- |
| `ok` | The provider and any postrun checks finished successfully |
| `error` | The provider, postrun check, or run failed |
| `timeout` | The provider turns exhausted their shared timeout allowance and were stopped |
| `no_work` | The prerun exited 1, or no task was available to a stage job; the provider never ran |
| `prerun_error` | The prerun failed; the provider never ran |
| `skipped` | The prerun opened, but the concurrency group was busy; the provider never ran |
| `running` | Still going, or interrupted before it could close |
| `cancelled` | A heartbeat assessment stopped before settlement, for example on pause, edit, or shutdown |

A timeout is drawn amber rather than red: the run did not fail, it ran out of time, and the
schedule chart is far easier to read when the two are distinguishable. Both count as failures
wherever failures are counted.

This is the main reason the viewer exists. A failed run's output is a wall of text that is
miserable to read in a chat message and fine to read in a browser.

### Health

Every section of [`enso doctor`](cli.md): config validity, the home and workspace audits,
provider paths and whether they resolve, transport extras, agent and optional viewer
service state, every `JOB.md`, current Heartbeat health, and knowledge/memory audits.
Note findings include paths, counts, and commands for detailed scoped audits; they check
structure and source validity, not factual truth. Heartbeat checks inspect saved state;
they do not execute gates or start an assessment. The [Memory](#memory) page offers a
read-only audit; recall and corrections use the [memory CLI](cli.md#memory).

Every section gets the same block, problems first: a heading with its verdict, and a panel of
what it found. Sections retain their context line even when findings exist, including note
counts and detailed audit commands. A section with nothing to report says `No issues found` on a green row rather
than disappearing, so it is clear it was checked; what it checked — which providers, which
workspaces — is the detail line on that row.

What the check actually read folds shut at the foot of the same panel, beside the verdict it
explains rather than in one heap of unrelated facts at the bottom of the page. That fold is
`Section.details` from `enso doctor --json`, drawn as what each value is: a path in code, a
flag as a word a person would use, a list of names on one line of them, and a per-item map
such as a provider or a transport as a labelled line with its flags and names quiet
underneath. Its chevron takes the finding rows' dot column, so the fold lines up with the
rows above it.

Plus the database footprint
(`enso.db` with its `-wal` and `-shm` companions), whether it is readable at a schema this
Enso knows, and the viewer itself: its version, the home it reads, where it is bound, and
that it is read-only. The last 200 lines of `enso.log` are the
Log section at `/health/log`. This page works when nothing else does: it is where a
missing or invalid `config.json` gets diagnosed.

## Access

The viewer binds `127.0.0.1` by default and has no authentication, because it assumes it is
only reachable from the machine it runs on. It answers `GET` and nothing else: every other
method, `HEAD` included, gets a 405. The only forms are read-only `GET` filters. Every
response carries a Content Security Policy that allows nothing but the viewer's own
stylesheet, script, icons, manifest, and safely served local knowledge images, and pages are
never cached.

`--host` will bind elsewhere, and you should not use it on an untrusted network. Everything
the viewer displays — job prompts, run output, workspace files — is content you would not
want to publish. If you want it on your phone, put it behind a private tunnel or a reverse
proxy that handles authentication, and leave Enso bound to localhost behind it.
