# Tasks

A task belongs to a project, sits in one stage, and keeps an append-only timeline in
`enso.db`. [Stage jobs](jobs.md#stage-jobs) pick up queued work; chat and `enso task` manage
it, and the [viewer](web.md#tasks) displays the board.

This page owns task moves, claims, workflow acceptance, worktrees, and the `task`, `project`,
and `workflow` commands. [Configuration](configuration.md#projects) owns `PROJECT.md` fields.

## The model

| Field | What it is |
| --- | --- |
| reference | `<KEY>-<number>`, such as `EN-041`: the project key and a number that counts up per project, zero-padded to three digits. Commands accept it loosely (`en-41` is `EN-041`). |
| title, body | The current request. Original and corrected request revisions are retained. The title is one line; the body is Markdown, often written from a file. Both are untrusted data: control characters are stripped, and the text is only ever bound as a parameter or printed inside a framed block. |
| stage | One of the project's own stages, or a built-in one: `backlog`, `blocked`, `done`, `cancelled` |
| priority | An integer, default 0; higher runs first within a stage |
| attention | A flag that a person should look; set by `note --attention` and by Enso, cleared by any move |
| after | A task this one waits on while blocked; `add --after` starts it blocked |
| from | The task this one was discovered in |
| claim | The run holding the task, its actor, and when; empty when nobody holds it |
| timeline | Events, newest first: `created`, `taken`, `released`, `moved`, `edited`, `noted`, `ref`, the workflow's `submitted`, `accepted`, `rules_approved`, `workflow_reset`, and `workflow_recovered`, and the worktree's `worktree_setup`, `worktree_adopted`, `worktree_teardown`, `worktree_removed`, and `worktree_cleanup`; each with its actor, run id, message, and payload |
| refs | Evidence attached to the task: a `commit`, `path`, `url`, `page`, or any lowercase kind, with an opaque value |

Tasks, events, refs, workflow transactions, check results, and lifecycle deliveries are never pruned. Finished tasks (`done` and `cancelled`) are hidden
from listings unless asked for.

## Projects and stages

A project is `workspaces/<workspace>/projects/<KEY>/PROJECT.md`. The directory supplies
its installation-unique key and workspace; frontmatter defines its display name and stages:

```yaml
---
name: Enso
workflow: 2
enabled: false
repo: ~/Projects/enso
base: develop
stages: [work]
setup: ./setup.sh
copy: [.env]
---
```

`name` is the display name and `stages` declares the order of all possible stages.
`paths` selects permitted subsets; without it there is one `default` path containing all stages.
New definitions start paused; validate and activate them with `enso workflow enable KEY`. A stage can be a string (`name` or `name:human`) or an object
containing execution and acceptance rules. An **agent stage** is served by a stage job;
a **command stage** runs a script without a model; an **integration stage** lets Enso
validate and land a Git candidate; a **human stage** waits for an operator. A repository
is optional. Repo projects create [worktrees](#worktrees) only for stages that need one. [Configuration](configuration.md#projects)
lists every validation rule.

Every project also has four built-in stages, which a project may not name:

| Stage | Meaning |
| --- | --- |
| `backlog` | Not ready. Ideas, rough tasks, work an agent spotted while doing something else. Never claimed. |
| `blocked` | Stopped and waiting on a decision or another task. Always in the viewer's Blocked group. A reason is required. |
| `done` | Finished; the selected path's last stage lands here |
| `cancelled` | Dropped by a person |

`enso project add` validates and atomically creates `PROJECT.md`; it refuses a key that
already exists anywhere in the installation. `--stages` spells out the pipeline and defaults
to one `work` stage. `enso workflow init` applies a preset to an existing project and creates
its stage jobs; see [Development preset](#development-preset).

```bash
enso project add EN --name Enso --workspace dev --repo ~/Projects/enso \
  --setup ./setup.sh --copy .env
enso workflow init EN --workspace dev --preset dev --base develop \
  --lint ./lint.sh --test ./test.sh
enso project add MKT --name Marketing --workspace marketing \
  --stages research,draft,review,approve:human,release
enso project list --all-workspaces
```

Create `setup.sh`, `lint.sh`, and `test.sh` beside the project definition before using the
repository workflow. The script pattern below enters the task code explicitly.

Stage responsibilities live in `inputs`, `output` and `instructions`.
The bound job independently chooses its agent triple and adds executor instructions. Required acceptance
checks and command stages live in the project definition, where Enso can execute them
independently. See [Jobs](jobs.md#stage-jobs).

<a id="project-files-and-scripts-in-020"></a>

### Project files and scripts

All project commands start in the directory beside `PROJECT.md`: setup, command stages,
acceptance checks, after-transition hooks, and teardown. A command that needs task code
explicitly enters the recorded `ENSO_TASK_DIR`. For the configuration example, `test.sh`
lives beside `PROJECT.md` and can contain:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "${ENSO_TASK_DIR:?This check requires a task worktree}"
uv run pytest
```

Use the same pattern for `setup.sh` to call a repository's `.dev/prepare`. Commands that
need task code require a worktree stage; non-Git commands can use the project directory.
Provider processes start in the owning workspace. A script holds the worktree while using it.

Task commands require `--workspace` or `ENSO_WORKSPACE`, even with a unique task reference;
the current directory never supplies context. Lists and `task sweep` support
`--all-workspaces`; `--all` only includes finished tasks. `--after` and `--from` can link
across workspaces without transferring ownership. Moving a project directory does not
reassign its tasks; inconsistent ownership is refused. Stage jobs must share their
project's workspace.

## Moves

Enso resolves every transition from the selected declared path. Intake, jobs, operator
commands, dependency resumes and recovery use the same resolver. A path is execution state;
`task edit` cannot change it. `task show --json` exposes it under `contract` with the required
inputs, accepted revisions, pending decision and available choices.

| Move | Effect |
| --- | --- |
| `advance` from backlog | Enters the selected path; `--route NAME` resolves intake when needed |
| `advance` from a stage | Submits the result and advances to the next stage on that path, or `done` |
| `return` | Revisits the stage's explicit earlier `return_to`, within `max_returns` |
| `block` | Waits with a reason; `--after REF` names a dependency |
| `resume` | Returns to the interrupted stage; `--to` may only name that same stage |
| `drop` | Cancels unfinished work; available to an operator |

A decision stage accepts `--route NAME` only from its declared `routes`. Without a selected
path, execution follows only the common prefix and waits at the divergence. A configured
`default_route` can resolve it; an unresolved choice never runs all alternatives. Stages
outside the selected path produce no run, accepted result or stage-entry reaction.

`enso workflow reroute REF --route NAME --message REASON` is an explicit operator action.
It retains the accepted prefix and enters the first missing or stale stage on the new path.
Results from that stage onward become stale; newly required planning or approval must happen.
It does not reset repair or return budgets. A changed request similarly invalidates results
that consume it; reroute to the same path to revisit its first stale result.

Advance and return need a useful message: what changed, evidence, and what comes next.
Inside a run they submit without releasing its execution owner. Enso checks and accepts after
writers stop. An operator can directly accept unchecked work; checked work uses `workflow
verify`, command stages use their job, and integration uses its configured engine stage.
`--force` cannot bypass acceptance or a live claim. Blocking and cancellation do not require
passing checks. Runs cannot move human stages, drop tasks or resume blocked work.

A human stage's `advance` requires `--approve DIGEST`, using the current approval value from
`task show`.
The digest identifies the exact input revisions, stage instructions and acceptance rules,
and, for repository work, the committed candidate. Supply the decision reason with
`--message`. Revision changes require a fresh decision. Returning with feedback declines
approval and revisits the declared producing stage.

Tasks blocked on a completed dependency resume to their interrupted stage through the same
rules. Cancellation leaves dependants blocked and flagged for attention. Legacy/paused tasks
remain inert. Blocks identify `decision`, `approval` (changed check files), or `failure`, also
provided to lifecycle scripts in `ENSO_BLOCK_KIND`.

## Claims and readiness

A task can run when its workflow is active, its task belongs to the current engine, and it is unclaimed in an executable stage (agent, command, or integration),
with no unfinished stage transaction or unfinished/failed lifecycle event. Human stages and
built-in stages never run. `after` only matters while blocked. Legacy tasks/jobs remain preserved and paused even after their project is replaced. The `task list --ready`
filter lists unclaimed tasks in executable stages; the scheduler additionally checks
transactions and lifecycle events before claiming one.

Claims belong to runs. When a stage job fires, Enso takes the ready task in that stage with
the highest priority, then the oldest, in one immediate transaction, so two runs can never
hold the same task. The claim is the run id and the actor `job:<workspace>:<job>`; it is what stops a
second job or a person from moving the task underneath the run.

Only the claiming run may move, release, or edit a claimed task's spec. A live execution
claim cannot be forced: stop its job and let recovery release it first. Priority and `after`
edits remain allowed on unfinished tasks.

A submitted handoff holds the claim through validation and repair. A run that ends without
one blocks the task with the cause as a `failure` and flags it for attention. If the task
already left its stage, the claim is released instead, with reason `run_ended`; the next
run's Task block reports that recovery and any uncommitted files, so the agent can read the
timeline before continuing.

`enso task release` clears a claim without moving. The claiming run can release it; a
person needs `--force` and no live execution. To explain why work must wait, block it instead.

## Who is acting

Actor identity is derived from the environment, never passed:

| Where | Actor |
| --- | --- |
| A job run (`ENSO_JOB` set) | `job:<workspace>:<job>` |
| A chat turn (`ENSO_ORIGIN_TRANSPORT` set) | `slack:U0AETSSDDEF` or `telegram:123456`; `unknown` when the id is empty |
| A terminal | `user:<login>` |

A chat actor's event also records the sender's display name, for reading the timeline; the
actor remains the identity the guards compare. `enso workflow verify` records the operator
who ran it, and its execution ID starts with `manual-`.

`ENSO_RUN_ID` says whether the command runs inside a job. That is what withholds `drop` and
`--force` from an agent, and what lets the claiming run move its own task. Both are read
from the calling process's environment, so they are guardrails for a cooperating agent, not
a security boundary: a command run with those variables cleared or changed is treated as
whoever it claims to be, and the timeline records the actor as reported.

## The Task block

Each claimed run receives its task reference, workspace, selected path, stage, available
moves, required input revisions and their saved content, expected output, pending choices,
recent feedback, refs, and working directory. It can read the complete timeline with
`enso task show REF --json`. The request and each input are framed as indented untrusted data.
Project instructions and the stage/job instructions follow the block.

For repository stages the block names the task worktree, its branch and target, and the main
checkout that the worker must leave alone. Provider processes start in their owning workspace
so home and workspace instructions and skills remain discoverable. Scripts receive
`ENSO_TASK`, `ENSO_WORKSPACE` and `ENSO_TASK_DIR` when applicable.

Submit a deliverable with `enso task advance REF --output-file PATH --message TEXT`.
A text file becomes a saved text result; a `.json` file uses the result contract below.
Omitting `--output-file` uses the handoff message as the result. A stage produces a plan or
brief without editing the request it consumed. The next executor receives accepted content
and revision IDs directly, without reconstructing its assignment from conversational history.

Transactions retain the exact job prompt and its checksum, stage instructions, project
instruction text, executor configuration and paths/checksums of discoverable home/workspace
skills. These identify the available instructions; they do not claim that every skill was
loaded by the provider.

## Stage transactions and checks

Checks are optional. A project with `"stages": ["work"]` is a valid `work → done` flow,
with no Git requirement and no mandatory tests or review. Required checks are an opt-in
contract: once configured, all must pass before a forward handoff is accepted.

1. Enso claims the task and records the selected stage, spec/workflow versions, starting
   revision, and execution directory.
2. The agent works and submits `advance` or `return` with a handoff, then finishes its turn.
3. After provider execution and job postrun work stop, Enso evaluates the submission.
   Forward handoffs run configured commands against the stable candidate; returning work
   uses the configured earlier stage and return budget, without requiring a broken candidate
   to pass its forward checks.
4. A repairable failure feeds actual check output back into the same task, within finite
   attempt/time limits. A new candidate needs fresh evidence. Exhaustion, interruption,
   stale inputs, or an infrastructure failure preserves the work and diagnostic for attention.
5. Enso records acceptance, moves the task, and enqueues lifecycle events atomically.
   No database write transaction remains open while commands or models execute.

An agent may instead `block` with a concrete reason. The task becomes blocked while its
execution claim remains held until the provider stops. Enso retains that reason as the
transaction's blocked handoff; it does not accept the stage or run additional checks.
Earlier check evidence stays intact. The provider attempt keeps its own outcome even
when the overall run ends in error because its stage was not accepted.

A check is a named command plus a timeout. Exit 0 passes; nonzero, missing/failed execution,
timeout, interruption, or a missing required result cannot pass. Commands may be shell,
Python, package scripts, or existing test tools; no report format or eval framework is
required. A model review is judgment, recorded as a handoff, not an executable check result.

Results bind to their captured input IDs, candidate and workflow definition. Acceptance
commits the output revision, routing decision, move, history and pending lifecycle events in
one SQLite transaction. Checks run outside that transaction. A stale input, changed workflow,
failed check or interrupted execution cannot produce an accepted result.

### Accepted outputs

Every request and stage output has an immutable ID, per-stage revision, SHA256, actor,
transaction reference, content and consumed input IDs. Corrections preserve older versions;
`valid: false` marks stale results. Invalidating a result also invalidates results that consume
it. Changed stage instructions, inputs or acceptance rules also mark its evidence and consumers
stale. Earlier unrelated evidence stays inspectable. No output is silently overwritten.

The JSON result accepts `text`, arbitrary JSON `data`, and optional `artifacts`:

```json
{"text":"Campaign draft", "data":{"audience":"subscribers"},
 "artifacts":[{"uri":"https://example.test/drafts/42", "revision":"v3"}]}
```

Each artifact needs a nonempty URI and immutable revision, version or checksum. Enso retains
that reference; it does not fetch or snapshot remote content. Use inline text for a file's
exact content or a revisioned reference for a larger file. Results are limited to 256 KiB.
Checks receive the submitted JSON in `ENSO_OUTPUT` and consumed IDs in `ENSO_INPUTS`.

Existing validation inputs (common test files and package/tool manifests) are protected: a
candidate that edits or deletes them needs a person's approval before it lands. New tests
are allowed, and check `protect` patterns add project-specific inputs. A project with an
integration stage checks once, when it lands work, counting only the candidate's own changes
against the target; approving earlier, such as during QA, lets it land without stopping. A
project without one checks at every checked stage. Unapproved changes block the task as
`approval`.

`enso workflow approve-rules REF --message "what changed and why it is acceptable"` pins the
reviewed content. When the task is blocked on exactly that, the same command continues its
handoff: Enso rechecks the stage and moves on with the original handoff, so no job can pick
the task up in between. Later changes need approval again, and approval never records a
check pass.

Repair and return budgets persist across runs and service restarts. `max_repairs` and
`max_returns` default to 2; editing inputs, rerouting or restarting does not reset them; zero disables the respective loop. `enso workflow retry REF
--message "why another attempt is justified"` is an explicit, audited operator reset;
it never turns a failed check into a pass. Resolve the cause before resuming blocked work.

These checks enforce the workflow through supported Enso commands. Actor variables are
not authentication, and worktrees are not a sandbox: an unrestricted process under the same
OS account can modify controller state. Stronger isolation requires OS/provider controls.
Passing checks prove their results, not exhaustive correctness.

## Workflow examples

The [development](../assets/workflows/development/PROJECT.md),
[marketing](../assets/workflows/marketing/PROJECT.md), and
[support](../assets/workflows/support/PROJECT.md) definitions demonstrate optional paths,
agent work, deterministic commands and human decisions. They start paused. Tests execute
these contracts with synthetic providers and local commands; the publishing example is a
local fixture, not a configured external service.

An input suffixed with `?`, such as `plan?`, consumes that accepted revision when available.
This lets a shared build stage use an approved plan on a planned path while the direct path
can omit planning. Required inputs without `?` must exist on every path using that stage.

## Development preset

`enso workflow init KEY --preset dev --lint COMMAND --test COMMAND --base BRANCH` configures
an existing repo project and creates its disabled stage jobs:

```
build (agent) → review (a second agent) → qa (a person) → merge (Enso) → done
```

| Stage | Responsibility |
| --- | --- |
| `build` | Docs, tests, and code in the task's worktree; required lint and test checks with up to two repairs. Blocks with one question when a decision is missing |
| `review` | Reads the diff against the task and repository rules. Returns to build at most once, or hands the person short "Changed / Try it" steps |
| `qa` | A person tries the task's worktree, approves any edited tests, then advances to merge or returns to build with what's wrong |
| `merge` | Enso rebases onto the target, reruns the checks, and fast-forwards the target; no model |
| `done` | Accepted and merged locally; a clean, no longer needed worktree is eligible for cleanup |

Only ready work belongs in `build`. Keep tasks that still need a decision in `backlog` and
advance them once settled, rather than paying an agent to discover the question. The review
job uses a different configured provider than build when one exists, and both prompts keep
handoffs short: four lines from build, under 500 characters from review.

A person hears about two stops: a block, and a task reaching `qa`. The preset writes
`notify.sh` beside `PROJECT.md` and runs it from `hooks["after:blocked"]` and
`hooks["after:qa"]`; it sends one line to the transport's notify target. Add `--to` to its
`enso message send` to choose another destination. A send that fails is logged in the hook's
output rather than failing the hook, which would hold the task until retried. The project's
own hooks win, and the preset refuses rather than replace a different existing `notify.sh`.

Inspect the generated configuration and disabled job prompts before enabling them; add
checks or human stages where useful. Integration never implies permission to push, deploy,
or publish. Use `--preset basic` for one unchecked `work` stage. The bundled `enso-projects`
skill helps configure either process.

## Lifecycle scripts

Task transitions and worktree lifetime are separate. Project `setup` prepares a fresh
worktree; `hooks.after_transition` runs after every move, and `hooks["after:done"]`
(or another stage name) reacts to that destination. `hooks.teardown` runs before worktree
removal. Required pre-transition checks belong in the stage's `checks` array.

Scripts receive the move itself: `ENSO_FROM_STAGE`, `ENSO_TO_STAGE`, `ENSO_MESSAGE` (the
handoff or block reason), `ENSO_BLOCK_KIND` for a block, and `ENSO_TASK_TITLE`. They act for
Enso rather than for whichever command delivered them, so a chat turn's or job's identity is
never passed on and an untargeted `enso message send` goes to the configured notify target.

`hooks["after:blocked"]` is where a project tells people that work needs them. Every stage
run that is not accepted blocks its task with the cause, so the hook sees agent blocks,
refused handoffs, and failed runs alike; `ENSO_BLOCK_KIND` says which. While the hook is
defined, the stage job sends no alert of its own for those runs
([Jobs § Alerts](jobs.md#alerts)). A hook on a human stage, such as `hooks["after:qa"]`,
tells people when work is ready for them.

After-transition events are durably enqueued with the move, including CLI and dependency
moves. They run in order after execution ownership permits it, in the project directory.
`ENSO_TASK_DIR` points to the recorded worktree when available (empty otherwise). A failing
reaction does not undo an accepted stage: its output, retry attempts, and attention state stay visible.
Known failures have up to three delivery attempts with a stable `ENSO_EVENT_ID`; scripts
must deduplicate external effects with that ID. A delivery interrupted while running becomes
`uncertain` and is not replayed automatically. After inspecting the external system, use
`workflow resolve-event REF EVENT delivered --message RECEIPT`, or select `retry` with a
reason to authorize another attempt. Budget reset does not resolve uncertainty. A lifecycle script cannot recursively move tasks through the supported CLI.
Worktree-using events must finish before cleanup; teardown failure preserves the worktree.
See [Configuration](configuration.md#projects) for fields and [CLI](cli.md#environment-for-agents)
for script context. Use lifecycle events for completion reactions rather than inferring
completion from provider output or job postrun success.

## Replacing an existing workflow

[Manual workflow replacement](migration.md#legacy-workflows-and-manual-replacement) owns
activation, preservation and adoption. Definitions and stage jobs without `workflow: 2`,
and tasks preserved by the engine migration, are legacy and cannot execute. Standalone
jobs remain independent.

`workflow init --preset basic|dev` authors a fresh paused definition and disabled jobs.
With existing stage jobs, `--migrate` explicitly replaces the definition and preserves their
files as `JOB.md.pre-workflow`; it does not translate legacy tasks or accept their old work.
Original files and checksums are also retained in `runtime/workflow-migrations/<operation>/`.
Interrupted installation stays behind its maintenance gate; rerun `workflow init KEY` to
resume the recorded plan. User edits made after interruption are never overwritten.

Inspect the resulting contracts and jobs, run `config check`, explicitly enable the project
and selected jobs, then deliberately adopt or recreate outstanding work.

## Worktrees

Repository stages can request one worktree per task. Stages that only plan or wait for a
person need not create one. Once created, the same directory survives implementation,
review, repairs, blocking, and human checkpoints.

| Property | Default and ownership |
| --- | --- |
| Location | `.worktrees/<REF>/` beside the project's `PROJECT.md`, outside the repository |
| Branch | `enso/<REF>` |
| Target | Project `base`, or the main checkout's current branch on first creation |
| Recorded identity | Repository, absolute path, branch, target, starting commit, preparation and cleanup status |

The default keeps each task's checkout with the project that owns it, next to its config
and scripts, and out of the repository, where linters, file watchers, and search would
otherwise find every copy. `worktree_root` overrides it with a path relative to the
repository, an absolute path, or `~`; for example, `"worktree_root": "../task-worktrees"`
puts task directories beside the repository. Git's administrative directory cannot contain
a worktree root.

Enso keeps the root out of `git status` through Git's local `info/exclude`, never the
tracked `.gitignore`: in the task repository when the root is nested there, else in the
repository that contains it, such as an Enso home kept in Git. A root inside the repository
can still trigger rebuilds in the main checkout, because exclusion does not configure every
development server, file watcher, or source scanner. Worktrees are full checkouts, often
with their own dependencies, and can reach hundreds of megabytes: anything that archives the
Enso home, such as a backup job, should skip `.worktrees/`. Agents start in their workspace,
which contains the default root, so a provider that loads instruction files from folders it
reads, such as Claude Code with `CLAUDE.md`, can load a task checkout's copy; set
`worktree_root` outside the workspace when a later stage must not see an earlier stage's
edits to those files that way. Change the configuration between active runs;
already-created tasks keep their recorded path, while new tasks use the new root.

Changing the project configuration does not move, forget, or retarget an existing task's
worktree. Preparation can record an existing registration at the configured path when its
branch matches the task. A registration on a different branch or an unregistered directory
with content is refused, with the directory left untouched. Missing worktrees can reattach
their recorded branch at the recorded path. Enso does not search other locations for worktrees.

### Preparation

Before the first writing stage, Enso records ownership, adds the worktree, copies selected
local inputs, and runs the configured `setup` command with bash beside `PROJECT.md`.
The script enters `ENSO_TASK_DIR` to prepare task code; supporting scripts stay beside the
definition. Output is bounded and the timeout is `script_timeout` (600 seconds by default). Setup must be
idempotent: a failed or interrupted setup is recorded and retried in the retained directory.
Enso preserves any files the failed setup created. Successful setup runs only once.

Setup and teardown receive the [project script environment](cli.md#environment-for-agents),
including `ENSO_TASK_DIR`, the recorded branch/base, and a stable `ENSO_EVENT_ID` for retry
deduplication. Both stage fields name the current stage. Preparation output and copy counts
appear in the timeline.

A repository `.worktreeinclude` selects **ignored files** to copy from the repository's
main checkout. It uses one repository-relative glob per line; directories include their
contents recursively, `**` matches directory levels, blank lines and `#` comments are
ignored, and a leading `!` excludes a matching path and its descendants regardless of order:

```text
.env
settings.local.json
local-fixtures/
!local-fixtures/private/
```

The project `copy` list adds explicit paths to these selections. Tracked files
already arrive through Git. Enso never overwrites an existing destination, follows a
symlink, copies Git metadata or a nested repository, or copies a registered worktree or the
nested worktree root. Copy-on-write filesystem clones are preferred; an ordinary independent
copy is the fallback, and its use is reported. No mutable dependency directory is shared
automatically. Use the project's package manager in setup to create environments that must
be installed at their own path, such as Python virtual environments.

### Integration and cleanup

The workflow's explicit integration stage owns landing. Enso serializes integration per
repository, rebases the task branch onto its **recorded target's current commit**, and checks
that candidate. Both candidate and target must still match when it lands. A changed target
requires another rebase and check; a changed candidate requires fresh evidence. Conflicts or
a timed-out rebase are aborted and retained for repair. Dirty candidates and dirty target
checkouts are refused. When the target is the main checkout's branch, Enso fast-forwards it;
when it is not checked out, Enso updates only that branch with a compare-and-swap. A target
checked out in another worktree is left alone. Landing never pushes or deploys.

Before updating Git, Enso saves the exact integration intent and its check evidence. It
records the observed Git outcome before accepting the stage. If execution stops between
those operations, operator recovery can recheck the already-integrated candidate against
the current target and then accept it. It does not reuse old passing checks or create a
second integration commit. The task history distinguishes an applying intent, an applied
Git change, and an accepted stage. Cancellation waits for any Git worker to stop before
releasing the repository or task execution lock.

`enso task land` remains available where the workflow permits it; configured acceptance
checks cannot be bypassed by moving or landing a task directly. A task's `done` stage means
whatever its configured workflow establishes, such as a verified local integration, rather
than a claim that it has been published.

One execution owner holds a task's worktree from preparation through agent work, checks,
repairs, and worktree-using lifecycle hooks. Other tasks can run up to the configured project
concurrency limit. The repository integration lock also coordinates different Enso homes.

`enso task sweep [--project KEY]` and the runner reconcile only Enso-owned, terminal tasks.
A live claim, execution owner, or pending/running/failed lifecycle event prevents cleanup.
Blocked tasks and human checkpoints retain their worktrees. Tracked changes, untracked
nonignored files, and branches not integrated into the recorded target are preserved with
an attention entry describing the reason.

If configured, `hooks.teardown` runs before removal. Failure preserves the worktree; after three failed attempts, `enso workflow retry`
with a reason is required before another retry. A successful teardown is recorded and is not repeated if Git removal needs a retry. Teardown
can archive valuable local artifacts. **Ignored files are removed with an otherwise clean
worktree**, so an ignored file is not a durable archive. Git removal never uses `--force`,
and the branch is deleted only after proving it is integrated into the recorded target.
An interrupted removal is reconciled from its recorded state on the next sweep.

Shared databases, ports, credentials, and external services still need project setup and
appropriate [concurrency groups](jobs.md#concurrency-groups).

## Evidence, notes, and edits

- `enso task ref REF KIND VALUE` attaches evidence. `KIND` is lowercase (`[a-z][a-z0-9-]*`):
  `commit`, `path`, `url`, `page`, or whatever fits; the value is opaque, non-empty text.
  Attaching a pair that exists is a no-op. `advance --ref KIND:VALUE` attaches as it moves.
- `enso task note REF TEXT` adds to the timeline without moving; `--attention` also flags the
  task for a person. A note is the one thing a finished task still accepts.
- `enso task edit REF` changes the title, body, priority, or `after` (`--after ''` clears it).
  Title and body edits follow the claim guard and record the old values on an `edited`
  event, so a spec can be corrected without losing what the agent was working from.
- `enso task add "Title" --project KEY --from REF` records work discovered while doing another
  task; `--backlog` parks it rather than putting it in the first stage, and `--priority N`
  orders it within its stage. `--after REF` makes it wait: the task starts out `blocked` on
  that reference, with `Waiting on REF` on its `created` event, and is resumed into the
  first stage when that task is done. With `--backlog` it is parked as asked and the
  dependency is kept for whenever it is blocked.

## The CLI

```text
enso task add TITLE --project KEY [--body TEXT | --body-file PATH|-] [--priority N] [--route NAME] [--backlog] [--after REF] [--from REF] [--workspace W]
enso task list [--project KEY] [--stage NAME] [--ready] [--claimed] [--attention] [--all] [--idle-for 30m] [--workspace W] [--all-workspaces]
enso task show REF [--workspace W]
enso task advance REF --message TEXT|- [--output-file PATH] [--route NAME] [--approve DIGEST] [--ref KIND:VALUE ...] [--force] [--workspace W]
enso task return REF --message TEXT|- [--force] [--workspace W]
enso task block REF --message TEXT|- [--after REF] [--force] [--workspace W]
enso task resume REF [--message TEXT|-] [--to STAGE] [--workspace W]
enso task drop REF --message TEXT|- [--workspace W]
enso task release REF --message TEXT|- [--force] [--workspace W]
enso task edit REF [--title T] [--body-file PATH|-] [--priority N] [--after REF] [--force] [--workspace W]
enso task note REF TEXT|- [--attention] [--workspace W]
enso task ref REF KIND VALUE [--workspace W]
enso task land REF [--workspace W]
enso task sweep [--project KEY] [--workspace W] [--all-workspaces]
enso workflow show REF [--workspace W]
enso workflow verify REF --message TEXT|- [--output-file PATH] [--route NAME] [--approve DIGEST] [--workspace W]
enso workflow enable KEY [--workspace W]
enso workflow reroute REF --route NAME --message REASON [--workspace W]
enso workflow adopt REF --route NAME --message REASON [--workspace W]
enso workflow resolve-event REF EVENT delivered|retry --message RECEIPT [--workspace W]
enso workflow retry REF --message TEXT|- [--workspace W]
enso workflow approve-rules REF --message TEXT|- [--workspace W]
enso workflow init KEY --preset basic|dev [--lint CMD --test CMD] [--base BRANCH] [--worktree-root PATH] [--migrate] [--workspace W]
enso project list [--workspace W] [--all-workspaces]
enso project add KEY --name NAME [--workspace WS] [--repo PATH] [--stages a,b,c:human] [--setup CMD] [--copy PATH]...
```

Every command takes `--json` and follows the [JSON error contract](cli.md): a refused move or
edit prints `{"ok": false, "error": "<reason>"}` and exits 1, and so does an unknown
reference or project. A message of `-` or `--body-file -` reads stdin, so a handoff longer
than a line needs no shell quoting; `--body` is literal text. Bodies and messages follow
the [CLI input limits](cli.md). `--idle-for` takes `30m`, `2h`, or `1d`, and keeps tasks
that entered their stage at least that long ago.

`list` hides `done` and `cancelled` unless `--all` is given or `--stage` names one of them,
and orders by project, then priority descending, then creation. `--ready` keeps unclaimed
tasks in executable stages; `--claimed` those a run holds; `--attention` those flagged. Text output
is a table of `REF`, `STAGE` (with `!` for attention), `PRIORITY`, `CLAIM`, `IN STAGE`, and
`TITLE`; `--json` is a list of task objects with every field of the model above
(`claim_run_id`, `claim_actor`, `claim_at`, `after_ref`, `from_ref`, `previous_stage`, and
the three timestamps included).

`show` prints the fields, available and refused moves, refs, body, and timeline. Its JSON
result is the [Task block](#the-task-block) context plus `events` (the full timeline,
newest first), `workflow` (durable stage transactions), and `contract` (path choices,
required inputs, output revision history, approval digest and pending action). Missing `claim`, `handoff`, and
`recovery` values are `null`; `notes` contains the newest five. `moves` omits `drop` inside
a run. Events carry their actor, run, timestamp, message, and relevant move, release,
edit, attention, claim, or reference details: a chat sender's `actor_name`, a claim's
`execution` (the run's kind, provider, model, and effort, kept after the run row is pruned),
and an accepted or interrupted move's `transaction_id`.

Task moves print one line or, with `--json`, the task object. An in-run advance/return
prints the retained stage because it submitted a handoff, not an already accepted move; `note` and `ref` print the event and the ref.
`project list --json` is a list of project objects with `key`, `name`, `workspace`, `repo`,
`stages` (objects with execution/check settings), `setup`, `copy`, `base`, `worktree_root`,
`max_concurrency`, `hooks`, and `script_timeout`; `project add --json` prints the
one it wrote.

## From chat

The agent in a chat turn uses the same commands, as the actor the origin names. "Move
EN-041 to review with a note that the fences are fixed" is `enso task advance EN-041
--message "…"`; "park it" is `block`; "kill it" is `drop`, which only a person's turn can
do. The bundled `enso-projects` skill teaches the agent the moves and the rules above; the
[web viewer](web.md#tasks) is where you read the board.
