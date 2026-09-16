# Enso workspace rewrite

**Status:** pending

**Purpose:** draft for review and refinement; implementation has not started.

**Planning baseline:** `develop` at `0bb02da`.

## Status legend

Use exactly these values for every phase and task:

| Status | Meaning |
| --- | --- |
| `pending` | Work has not started. |
| `in-progress` | Work has started, or implementation still needs validation or a commit. |
| `complete` | Requirements and validation are satisfied, and the task's implementation commit is recorded. |

A phase becomes `in-progress` when its first task starts and `complete` only when all its
tasks are complete. A blocked task remains `in-progress`, with the blocker described in
its notes. Use `—` for an unknown commit; never invent a hash or mark uncommitted work complete.

## Why we are doing this

Enso should be a helpful agent for a person or a small team trusted to use it. People
should be able to ask it to remember something, finish work, or follow up later without
understanding databases, schedulers, provider settings, or how its files are arranged.

One Enso installation is one trusted working environment. Teams that need strict separation
run separate Enso installations on separate machines or VPSs, with their own credentials,
provider logins, data, and chat connections. For example, the development team's Enso and
the finance team's Enso do not need to know about each other. Separate installation names
or directories on the same unrestricted account do not provide that separation.

Within an installation, workspaces help Enso focus. Each workspace owns its conversations,
memories, jobs, and work. A channel or conversation selects that context for the user.
Workspace boundaries describe ownership and relevance; they do not promise confidentiality
from other agents running in the same installation.

This lets us remove Enso's `restricted`/`unrestricted` workspace distinction and avoid
building containers, permission brokers, or a second security policy system into the core
product. Provider permissions can remain configurable, and Enso must still control who
can use the installation, protect credentials, handle untrusted input, and preserve user data.

Files that people and agents maintain should be ordinary Markdown. Knowledge describes
current facts and useful reference material. Memory records dated experiences and context.
Knowledge can belong to the installation or a workspace. Memory belongs only to a workspace.

Operational records belong in one SQLite database. Captured conversations, task state,
job results, and processing receipts benefit from structured queries and transactions.
They do not need a separate database for each workspace now that isolation happens between
installations. The maintained Markdown notes remain the source of truth for knowledge and memory.

We should also remove settings that repeat information already established by location.
A job inside a workspace belongs to that workspace; its frontmatter should not need to
say so again. Internal database records still need explicit ownership so Enso can route,
query, and recover work correctly.

Gavin's local installation is the only installation that needs adaptation. We can document
and perform a deliberate manual conversion when the rewrite is ready. We do not need a
general migration framework or compatibility layers for a population of older installations.

## Target structure and responsibilities

The main content structure is:

```text
~/.enso/
  config.json                   # installation settings, providers, transports, bindings
  AGENTS.md                     # shared working guidance
  enso.db                       # operational records for this installation
  knowledge/                    # shared Markdown reference material
  skills/                       # shared skills
  workspaces/
    <name>/
      AGENTS.md                 # purpose and working conventions
      knowledge/                # Markdown reference material owned by this workspace
      memory/                   # dated Markdown memories; no top-level memory root
      jobs/
        <job-name>/
          JOB.md                # job definition; workspace comes from its location
          prerun.sh             # optional
          postrun.sh            # optional
      projects/                 # optional project and workflow definitions
      heartbeat/                # optional scripts for follow-ups owned by this workspace
      drafts/
      uploads/
      skills/                   # optional skills specific to this workspace
```

This shows content ownership, not every generated file. Existing installation runtime,
logs, private configuration, locks, caches, instruction links, and provider discovery
support still need a documented home. Do not relocate working support files just to make
this diagram exhaustive. Provider authentication stays with the provider CLI on the machine.

| Resource | Source of truth | Ownership |
| --- | --- | --- |
| Installation configuration and chat access | Home configuration | Installation |
| Shared skills and shared knowledge | Files under the home | Installation |
| Workspace guidance, knowledge, memory, drafts, uploads | Files under the workspace | Workspace |
| Job definitions and their supporting scripts | Workspace `jobs/` | Containing workspace |
| Project/workflow definitions and supporting files | Workspace `projects/` | Containing workspace |
| Captured exchanges, sessions, tasks, runs, follow-ups, outbox records | One home SQLite database | Workspace ownership recorded or unambiguously linked internally |
| Follow-up gate scripts | Workspace `heartbeat/` | Workspace recorded on the follow-up |
| Registered user tables | Existing home SQLite database | Installation; finer organization only when a real use requires it |
| Service, scheduling, updates, and installation health | Host runtime and operational state | Installation |

There is no required privileged workspace type. Installation-maintenance jobs can live in
an ordinary default workspace. External source repositories can stay at their existing
paths; workspace ownership does not require moving a code repository into the Enso home.

## Scope and design limits

- Keep the normal experience conversational. Workspace selection usually comes from routing,
  and users should not need to manage internal identifiers or storage formats.
- Retain useful existing behavior instead of rewriting functioning modules unnecessarily.
- Keep one service and one operational database. Do not introduce per-workspace databases,
  containers, VM management, or access-control services in this rewrite.
- Remove Enso's workspace restriction mode without silently disabling provider safeguards
  or replacing the user's provider arguments with blanket bypass flags.
- Preserve installation access checks, input validation, safe file operations, bounded
  subprocesses, cancellation, conflict detection, and explicit authorization for external actions.
- Default lookup to the relevant workspace where appropriate. Broader installation-wide
  lookup remains available and is a context choice, not a privilege escalation.
- Scope database queries for correctness. A workspace filter is not a security boundary.
- Keep the existing viewer working through necessary data and routing adjustments. No new
  memory viewer, visual redesign, or unrelated web UI project is required here.
- Do not combine jobs, tasks, and Heartbeat into a new abstraction merely to reduce names.
  Their user-facing purposes can remain distinct while ownership and execution are consistent.
- Do not build automatic historical import, cross-installation synchronization, or automatic
  Git commits for notes. Existing retained data must be handled deliberately, never discarded silently.
- No implementation, live-home conversion, service restart, push, or release is performed
  merely by creating this plan. Each requires the appropriate subsequent task authorization.

## Execution and commit rules

This is the user-requested plan for this rewrite. Maintain progress here rather than creating
a second task-board copy. The explicit request for `PLAN.md` takes precedence over the
repository's normal preference against planning-state files.

1. Refine this draft before beginning the rewrite. Resolve the format and lifecycle choices
   in Phase 1 before implementing the parts that depend on them.
2. Work through tasks in order, one task at a time. Each task must be independently reviewable
   and committed before the next task starts. Split a task before starting if it cannot fit
   into one coherent implementation commit. Keep existing task IDs stable when adding tasks.
3. Set the active task and phase to `in-progress`. Record decisions, blockers, and validation
   evidence in the task's notes without changing the meaning of the status legend.
4. Include the related implementation, meaningful tests, owning documentation, and any
   relevant changelog update in that task's commit. Do not defer necessary correctness or
   documentation work to the final sweep. Earlier task boundaries should remain runnable.
5. Run the applicable checks from [Development](docs/development.md#setup-and-checks), review
   the diff, and create a Conventional Commit. Use an isolated scratch Enso home for tests.
   Documentation-only tasks need document/link/diff checks, not application tests without a
   relevant behavior change.
6. After committing, record the full implementation hash in the task's `Commit` field and
   set the task to `complete`. Include test commands/results and any remaining limitations
   in `Notes`. A task is complete only if none of its requirements remain outstanding.
7. Commit that progress update separately as a plan-only documentation commit before starting
   the next task. A commit cannot contain its own final hash; do not amend it to try to make
   that work. The task's `Commit` field identifies the implementation commit, not the tracking commit.
8. If later work invalidates a completed requirement, reopen the task or add a focused follow-up
   with its own ID and commit. Do not rewrite recorded history or hide unfinished work.
9. Follow the repository's branch and release rules. The planning checkout is ordinary
   `develop`; this document does not authorize new branches, worktrees, merges, pushes,
   history rewrites, or publication. Completion is distinct from installation or release.

Each phase below lists the owning documentation to update alongside its tasks. New
`docs/memory.md` will own memory and capture behavior; this plan will remain a progress
record rather than a competing permanent product specification.

## Phase 1 — Settle the product and data contracts

**Status:** pending

**Depends on:** review of this draft.

**Owning docs:** [Concepts](docs/concepts.md), [Configuration](docs/configuration.md),
[Workspaces](docs/workspaces.md), [Knowledge](docs/knowledge.md), new `docs/memory.md`.

### P1.1 — Define installation trust and workspace ownership

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Document one installation as one trusted environment and separate machines/VPSs as the
    deployment model for teams requiring separation. The host and its administrators remain trusted.
  - Define workspaces as context and ownership, with no confidential compartment claim.
  - Specify how channel bindings, explicit CLI selection, and the current workspace select
    context. Missing or ambiguous ownership must produce a useful error instead of guessing.
  - Set the default workspace name for fresh setup and the default home for maintenance jobs.
  - Define job references as workspace plus local job name. Choose and document project
    reference rules; prefer keeping globally unique project keys and existing simple task
    references unless review establishes a need for another namespace.
- **Validation:** Walk through a personal default workspace, two team channels in one instance,
  and two entirely separate team installations. Every resource in the ownership table has one owner.
  Clearly mark proposed contracts as forthcoming until their implementation tasks are complete.
- **Notes:** —

### P1.2 — Specify note formats and memory lifecycle

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Define exact required and permitted frontmatter for knowledge and memory, with a purpose
    for every field. Use stable IDs and versioned schemas. Do not add scope or access settings
    to frontmatter when the path already determines ownership.
  - Review the existing knowledge fields (`schema`, `id`, `created`, `updated`), including
    the treatment of imported notes with unknown dates. Never invent historical timestamps.
  - Define memory occurrence time separately from file creation/edit times, the date-folder
    convention and timezone, and how later corrections preserve historical meaning.
  - Define what one memory note represents, how generated notes retain source references,
    and how source expiry differs from a broken reference. Avoid rigid boilerplate without a use.
  - Specify how direct human edits, imported files, CLI edits, links, and duplicate IDs are handled.
  - Set capture enablement defaults, harvesting cadence/batch bounds, and retention behavior.
    Distinguish raw-capture retention, memory retrieval relevance, and actual note deletion.
  - Define whether session clear affects capture or memory, and how an explicit forget/delete
    request is handled. Prefer separate, clearly named operations over surprising implicit deletion.
  - Define capture-write failure behavior, partial replies, user edits/retries, attachment-only
    turns, rich responses, and separate outbound sends. Set limits and make truncation explicit.
- **Validation:** Add representative examples and decision notes to the owning docs; all choices
  above have a definite answer before their dependent implementation begins.
- **Notes:** These details remain open for review; earlier conversational examples are proposals.

## Phase 2 — Simplify configuration and establish workspace context

**Status:** pending

**Depends on:** Phase 1.

**Owning docs:** [Configuration](docs/configuration.md), [Connections](docs/connections.md),
[Workspaces](docs/workspaces.md), [CLI](docs/cli.md).

### P2.1 — Remove the Enso workspace restriction mode

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Remove `workspaces.<name>.restricted`, its provider prerequisite gates, and related
    mode-dependent CLI, setup, audit, and bundled-guidance behavior.
  - Retain provider argument configuration and native provider permission behavior. Do not
    change invocation privileges as an incidental consequence of removing the flag.
  - Define clear handling of an obsolete `restricted` property during configuration loading;
    document the intentional format change without building a legacy policy subsystem.
  - Preserve transport pairing, allowed-sender/channel checks, and handling of unauthorized input.
  - Update affected security guidance to describe the installation trust model accurately.
- **Validation:** Configuration and launch tests cover chat, jobs, and Heartbeat without the
  removed gates; unauthorized incoming messages still cannot start work.
- **Notes:** —

### P2.2 — Add workspace-owned paths and consistent context resolution

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Add the target workspace paths through the existing path helpers; scaffold required
    directories and create optional ones only when their feature is used.
  - Keep one home database and preserve existing provider instruction/skill discovery.
  - Implement the context precedence agreed in P1.1 and pass the resolved workspace through
    service operations rather than repeatedly inferring ownership from changing configuration.
  - Distinguish data derived from a file's location from ownership that must be stored in a
    database record. Environment variables remain context hints, not authenticated identities.
  - Preserve safe path traversal, symlink handling, and user-authored files during setup/audit.
- **Validation:** Scratch-home setup, workspace creation, context ambiguity, path safety,
  and shared/workspace skill discovery behave according to the new layout.
- **Notes:** —

## Phase 3 — Consolidate jobs, projects, and operational ownership

**Status:** pending

**Depends on:** Phase 2.

**Owning docs:** [Jobs](docs/jobs.md), [Tasks](docs/tasks.md), [Heartbeat](docs/heartbeat.md),
[Configuration](docs/configuration.md), [CLI](docs/cli.md), [Workspaces](docs/workspaces.md).

### P3.1 — Move job definitions and execution references into workspaces

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Discover jobs under `workspaces/<name>/jobs/<job>/JOB.md` and derive their workspace
    from the containing directory. Remove the redundant frontmatter property.
  - Update creation, listing, showing, manual execution, scheduling, and run-history references
    together. Permit identical local job names in different workspaces without ambiguity.
  - Make job locks, scheduling state, failure fingerprints, and execution identity refer to
    the complete job identity. Define any intentionally installation-wide concurrency groups.
  - Run scripts from their documented directories and providers in their owning workspace;
    retain bounded output, timeouts, cancellation, follow-ups, and disabled-job behavior.
  - Adapt existing viewer readers/routes only as needed to keep job history usable.
- **Validation:** Two same-named jobs in different workspaces retain independent state;
  scheduling/manual-run exclusion and pre/post-run success, failure, and cancellation still work.
- **Notes:** —

### P3.2 — Make project definitions and task operations workspace-owned

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Store project/workflow definitions under the owning workspace's `projects/`, with one
    documented canonical format. Derive workspace ownership from location.
  - Keep task bodies, timelines, claims, workflow transactions, and receipts in the home DB.
  - Implement the project/task reference scheme agreed in P1.1 and reject duplicate or
    ambiguous definitions instead of choosing one arbitrarily.
  - Update project creation, task lookup, stage-job binding, and workflow execution together.
    A stage job must serve a project in the same workspace unless a separately documented
    cross-workspace operation explicitly transfers work.
  - Preserve task acceptance checks, worktree ownership, bounded repair, and existing deliberate
    task dependencies. External repositories remain at their configured paths.
  - Keep existing task/project viewer reads consistent without adding a new UI design.
- **Validation:** Tasks resolve to the correct workspace through a complete stage run;
  claims, dependencies, rejection/repair, and external-repository behavior remain correct.
- **Notes:** —

### P3.3 — Place follow-up scripts and state in their workspace context

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Locate Heartbeat gate scripts and helpers under the follow-up's owning workspace.
    Continue storing definitions, events, runs, and action receipts in the single database.
  - Resolve ownership at creation, persist it, and use it for later scheduling, file paths,
    provider sessions, and commands. Avoid repeating it in user-authored input when ambient
    context already selects it; retain explicit selection for standalone administration.
  - Preserve one-shot timing, recurring checks, idempotent action receipts, notification
    destinations, and paused/finished behavior.
  - Update existing reader paths and bundled guidance affected by the relocation.
- **Validation:** A scheduled follow-up survives restart and runs from its recorded workspace;
  gate failures, action retries, and cancellation cannot redirect or duplicate work.
- **Notes:** —

### P3.4 — Make conversation, outbox, and run ownership consistent

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Audit runtime records for missing or ambiguous workspace ownership and store or link it
    explicitly where needed. Do not duplicate an authoritative relationship without a use.
  - Freeze the workspace for each accepted turn and execution. Later binding changes must
    not move existing records or resume a provider session in the wrong workspace.
  - Define background-message handling across binding changes so old context does not
    accidentally appear as work belonging to a newly selected workspace.
  - Make ordinary context-sensitive lists/searches use the selected workspace; provide
    intentional broader views without presenting filters as security permissions.
  - Keep operational database access shared and treat output, error, and debug content as
    potentially private to the installation.
- **Validation:** Interleaved conversations in two workspaces, queueing, rebinding, and
  out-of-band replies retain the correct ownership and session behavior.
- **Notes:** —

## Phase 4 — Define reliable Markdown knowledge and memory

**Status:** pending

**Depends on:** Phases 1–3.

**Owning docs:** [Knowledge](docs/knowledge.md), new `docs/memory.md`, [CLI](docs/cli.md),
[Workspaces](docs/workspaces.md).

### P4.1 — Align knowledge with the agreed note contract

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Implement the metadata rules settled in P1.2 using the existing knowledge machinery
    wherever it already satisfies them. Retain shared and workspace knowledge roots.
  - Preserve stable IDs, useful Markdown links, attachments, atomic publication, conflict
    checks, and reliable move behavior. Do not replace working behavior merely for symmetry.
  - Make the distinction between current maintained knowledge and dated memory clear in
    authoring guidance and scope selection.
  - Report invalid/imported files without silently rewriting them or fabricating metadata.
- **Validation:** Representative create/read/edit/import/move/link flows, invalid metadata,
  duplicate identity, and concurrent edits meet the documented contract.
- **Notes:** —

### P4.2 — Implement workspace Markdown memory and its CLI

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Add workspace-only memory roots, chronological placement, and the exact schema from P1.2.
    There is no shared/top-level memory root.
  - Provide the minimal read, list, search, create/update, and validation operations needed
    for manual memory maintenance and later harvesting. Final command syntax belongs in the CLI docs.
  - Keep Markdown authoritative. Any parsed cache or search index must be rebuildable from files.
  - Preserve identity, source context, occurrence time, and corrections through supported edits.
  - Reuse shared parsing/storage primitives only where behavior actually matches knowledge;
    retain distinct rules where history and current reference differ.
- **Validation:** An agent and a human can maintain a dated note, find it later, correct it,
  and recover from conflicting edits without corrupting its identity or event date.
- **Notes:** —

## Phase 5 — Capture conversations reliably in SQLite

**Status:** pending

**Depends on:** Phases 1–4.

**Owning docs:** new `docs/memory.md`, [Concepts](docs/concepts.md), [Configuration](docs/configuration.md).

### P5.1 — Add exchange storage and processing state

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Store original user text and normalized final reply text in the existing home database,
    with stable exchange identity, workspace, conversation/source identity, timestamps,
    completion/delivery state, and only the provenance needed by the agreed contract.
  - Support saving the request before provider execution, then recording the generated reply
    and its delivery result without falsely treating them as the same event.
  - Implement deduplication, bounded workspace/conversation queries, and processing receipts.
    A retried event cannot silently create another exchange in a different workspace.
  - Represent failure, cancellation, incomplete capture, and truncation explicitly. Follow
    P1.2 for size limits and capture failures; report storage problems without exposing message bodies.
  - Define a clear schema upgrade for the development baseline and scratch installs; do not
    recreate the abandoned memory-summary database as a second source of truth.
- **Validation:** Persistence tests cover interruption between state changes, duplicate events,
  chronological retrieval, workspace filters, limits, and independent processing progress.
- **Notes:** —

### P5.2 — Connect capture to transport admission and final replies

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Capture only accepted, authorized messages after their workspace is resolved, early enough
    to preserve requests waiting in the queue. Follow the agreed behavior if durable capture fails.
  - Capture the original user request, not the assembled provider prompt. Exclude injected
    history, background context, system guidance, tool calls/results, progress messages, and
    internal formatting-repair turns.
  - Record the final user-visible response using a documented representation for rich output.
    Preserve generation and delivery outcomes, including partial or uncertain delivery.
  - Keep attachments as workspace-owned references according to the agreed contract; do not
    silently scrape or copy arbitrary files into capture records.
  - Mark stopped/dropped/incomplete turns appropriately. Capture recovery must never resend a
    message or rerun a provider merely to fill a missing record.
  - Support Slack and Telegram through the common runtime contract, without depending on a
    transport's ability to retrieve historical messages.
- **Validation:** Fake-transport/provider tests cover normal and rich replies, retries, queued
  turns, stop, timeout, provider errors, empty replies, delivery failure, and changed bindings.
- **Notes:** —

## Phase 6 — Turn captures into useful, recoverable memory

**Status:** pending

**Depends on:** Phases 4–5.

**Owning docs:** new `docs/memory.md`, [Jobs](docs/jobs.md), [Knowledge](docs/knowledge.md),
[CLI](docs/cli.md).

### P6.1 — Implement bounded workspace harvesting and recovery

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Select bounded, ordered captures from one workspace and preserve conversation boundaries.
    Harvesting does not fetch global transport history or mix another workspace into a batch.
  - Treat source conversation text as untrusted evidence. A summary must distinguish user
    statements, agent suggestions, attempted work, and confirmed outcomes.
  - Validate generated notes against their schema, source IDs, and selected workspace before
    publishing them. An empty useful result is a valid processed outcome when recorded explicitly.
  - Use stable output identities and processing receipts so concurrent runs and retries do
    not duplicate notes or lose captures.
  - Handle the gap between a Markdown file write and a SQLite transaction explicitly: after
    a crash, reconcile published files and receipts before retrying. Do not claim both writes
    form one atomic transaction.
  - Mark inputs handled only once their validated output or documented no-memory result is
    durable. Preserve conflicting human edits rather than overwriting them during recovery.
- **Validation:** Exercise concurrent harvesters and interruption before publication, after
  publication, and before receipt completion. Every input remains recoverable or accounted for.
- **Notes:** —

### P6.2 — Add the memory skill and workspace harvesting job

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Provide concise `enso-memory` guidance for finding, recording, correcting, and refining
    memory, with actual tested CLI examples.
  - Install harvesting jobs under their owning workspace and apply the enablement/cadence
    decision from P1.2. The service can schedule all of them without a global mixed-content batch.
  - Keep runtime job prompts and scripts small; reusable validation and recovery belong in
    code, and configurable writing preferences belong in the skill.
  - Describe deliberate promotion of useful lasting facts into the appropriate knowledge
    note, with source context and no automatic duplication of every memory into knowledge.
  - Ensure job follow-up text and output are not recursively captured as new user conversations.
- **Validation:** A complete fixture conversation becomes a useful Markdown memory, a repeat
  run creates no duplicate, and a quiet workspace causes no unnecessary provider invocation.
- **Notes:** —

### P6.3 — Implement the agreed retention and forgetting behavior

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Implement the retention choices from P1.2 with clear previews or reports for destructive
    operations. Do not choose arbitrary expiry periods during implementation.
  - Protect unprocessed or unresolved captures from ordinary retention cleanup.
  - Keep memory note lifetime separate from capture lifetime. Source expiry must remain
    distinguishable from corrupted or never-existing provenance.
  - Apply explicit forget/delete behavior to the documented scope; session reset must not
    silently imply a different deletion policy.
  - Document the effect of backups and Git history: deleting an active record is not a claim
    that every retained historical copy has been securely erased.
- **Validation:** Retention boundaries, unfinished batches, expired sources, explicit deletion,
  and session reset behave exactly as documented using synthetic data.
- **Notes:** —

## Phase 7 — Make setup, guidance, and diagnostics consistent

**Status:** pending

**Depends on:** Phases 2–6.

**Owning docs:** [Install](docs/install.md), [Workspaces](docs/workspaces.md),
[Customization](docs/customizing.md), [Knowledge](docs/knowledge.md), new `docs/memory.md`,
[CLI](docs/cli.md), [Web viewer](docs/web.md), [README](README.md).

### P7.1 — Integrate knowledge and memory checks into doctor

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Have `enso doctor` compose the knowledge and memory audits with clear counts and paths;
    keep detailed scoped audit output available through the relevant commands.
  - Check required frontmatter, supported schemas, identity uniqueness, timestamp rules,
    wrong-root schemas, memory date placement, links, and source validity/expiry as specified.
  - Report invalid roots, unreadable/corrupt files, and unsupported placement without following
    unsafe paths or automatically rewriting/deleting content.
  - Distinguish structural checks from semantic truth: doctor cannot prove that a fact is true
    or that a note was filed in the most useful workspace.
  - Bound normal output and avoid needlessly rescanning unchanged content when existing
    caching mechanisms suffice. Do not add a separate authoritative note database.
- **Validation:** Valid and deliberately malformed scratch homes produce useful, deterministic,
  read-only reports with correct exit status and JSON structure.
- **Notes:** —

### P7.2 — Update scaffolding, bundles, and conversational guidance

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Fresh setup and workspace creation produce the target layout and the selected default
    workspace, with appropriate jobs installed in actual workspace directories.
  - Adapt bundled audit/update jobs, installation manifests, and managed bundle paths to the
    new ownership scheme. Preserve disabled/customized files according to the documented rules.
  - Update shared/workspace instructions and affected skills so the agent can carry out
    ordinary requests without teaching users internal storage or scheduler terminology.
  - Keep provider login on the host and verify shared/workspace skill discovery still works.
    Preserve the source checkout's `CLAUDE.md` symlink convention.
  - Provide actionable messages for obsolete layouts without adding a general migration engine.
- **Validation:** Fresh setup and a second workspace are usable with packaged artifacts;
  bundle refresh does not overwrite customization or accidentally enable disabled work.
- **Notes:** —

### P7.3 — Finish command, reader, and documentation consistency

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Sweep README, owning docs, CLI help/JSON examples, bundled skills, setup text, and tests for
    obsolete global-job paths, redundant ownership fields, and workspace-isolation claims.
  - Ensure existing viewer pages can still read jobs, tasks, follow-ups, workspaces, and
    knowledge through their updated models and references. Limit UI changes to correctness.
  - Document capture/memory commands and common conversational workflows in their owning pages.
  - Add the intentional behavior/format changes to the changelog without assigning a release
    version or publishing anything.
  - Keep this plan as task history; permanent product rules belong in the owning docs.
- **Validation:** Check internal links, help examples, JSON contracts, packaged guidance, and
  existing viewer routes. No stale instructions require removed modes or locations.
- **Notes:** —

## Phase 8 — Verify the whole experience and prepare local adoption

**Status:** pending

**Depends on:** Phases 1–7.

**Owning docs:** [Development](docs/development.md), [Install](docs/install.md),
[Concepts](docs/concepts.md), new `docs/memory.md`.

### P8.1 — Validate complete workflows in isolated homes

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Test a conversation being routed, captured, refined into memory, recalled, and deliberately
    promoted into knowledge, using synthetic messages and fake providers/transports.
  - Test two workspaces with overlapping job names and simultaneous work; ownership remains
    correct through scheduling, queueing, task workflows, follow-ups, and restart recovery.
  - Exercise unauthorized input, malformed notes, untrusted captured instructions, unsafe paths,
    output limits, interruptions, and concurrent changes at the boundaries introduced here.
  - Verify packaged installation behavior in a scratch home and keep real credentials,
    external messages, provider accounts, and system service management out of automated tests.
  - Run the complete documented check suite and address concrete failures. Record the
    behavior proved and the limits of the evidence; do not claim workspace security isolation.
- **Validation:** `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  and `uv run pytest`, plus the relevant isolated packaged-install check, all pass.
- **Notes:** —

### P8.2 — Prepare the manual local conversion runbook

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Prepare a short manual runbook for Gavin's installation: backup, stop/drain, adapt config,
    move job/project/follow-up files, preserve disabled jobs and user edits, and verify data.
    Keep a machine-specific inventory and sensitive details outside the repository.
  - Specify a consistent SQLite backup together with the necessary files and configuration,
    a usable rollback, and reconciliation rules that protect work accepted after the backup.
  - Preserve existing knowledge and useful runtime data. Treat any import from the archived
    old memory database as a separate explicit decision rather than silently resurrecting it.
  - Rehearse the transformation with synthetic data in a scratch home. Do not build a general
    migration framework or make live changes as part of preparing the runbook.
- **Validation:** The rehearsed procedure reaches the target layout, preserves the chosen
  data and disabled jobs, and has a documented, usable recovery path.
- **Notes:** —

### P8.3 — Install and verify the rewrite in Gavin's local environment

- **Status:** pending
- **Commit:** —
- **Requirements:**
  - Perform live changes only after authorization to install this rewrite. Follow the committed
    runbook, use a recorded implementation commit, and retain a usable rollback.
  - Reinstall using the project's pinned development-install process and verify services,
    provider authentication, doctor, and representative real usage with Gavin.
  - Keep the old disabled memory job disabled until its replacement has been validated and
    activation is part of the approved local adoption. Do not resume a stale job definition.
  - Commit the sanitized runbook and verification outcome for this task. Record the installed
    code hash separately from this documentation commit so the running version is unambiguous.
- **Validation:** Record the actual installed hash, backup location privately, service results,
  doctor outcome, and the observed chat/job/memory/knowledge checks. Leave the task `in-progress`
  if live validation or Gavin's requested usage check is still outstanding.
- **Notes:** —

## Completion criteria

The rewrite is complete when every phase and task above is `complete`, every task has its
implementation commit recorded, and the following are true:

- The product consistently describes one trusted installation with workspaces for focused work.
- Enso has no `restricted` workspace mode; installation access and other retained safeguards work.
- Jobs and other workspace-owned definitions live in their workspace and avoid redundant ownership input.
- Operational state uses one database; knowledge and maintained memory use validated Markdown.
- Accepted conversations can be captured without fetching transport history, and memory
  processing is bounded, traceable, and recoverable without losing or duplicating work.
- Doctor checks knowledge and memory structure without pretending to verify semantic truth.
- The ordinary user can ask for useful work and follow-through without managing these internals.
- Required checks pass and the approved local installation has been verified. Release publication,
  container isolation, and UI redesign remain separate work.
