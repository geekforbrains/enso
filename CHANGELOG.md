# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- `enso policy list`, `show`, and `create` provide the supported policy lifecycle. List
  output summarizes capabilities, consumers, and validation; detail output adds safe
  path, revision, warning, environment-name, and MCP-server metadata without native
  policy contents or secret values. Creation requires
  exactly one explicit authority source (`--unrestricted` or an existing
  `--policy-dir`), repeated explicit providers, a default provider, and deliberate
  chat-command/environment choices; it validates the complete candidate under the config
  lock before saving. Fresh setup's full-authority unrestricted `admin` remains the sole
  automatic policy creation. Restricted canonical source content is authored and
  protected first, remains user-owned, and is never generated, copied, changed to
  different permissions, rewritten, upgraded, or repaired by Enso. `enso config check`
  remains the one complete validator, with no delete, repair, rebind, or preset surface.
  The new portable `policy` skill teaches the workflow and treats source examples as
  untrusted explanatory starting points.
- `enso workspace list`, `show`, `create`, and `repair` provide the supported workspace
  lifecycle. Creation requires a lowercase kebab-case name and an explicit existing
  policy, derives `~/.enso/workspaces/<name>`, defaults concurrency to `1`, validates the
  complete candidate catalog under the config lock, atomically publishes a staged
  scaffold, atomically saves config, performs the installation check, and snapshots its
  exact five versionable entries (`AGENTS.md`, `CLAUDE.md`, `.agents/skills`,
  `.claude/skills`, and `knowledge/README.md`). It has no path option and never grants `admin`
  implicitly; fresh setup's initial unrestricted `admin`/`default` binding remains the
  sole automatic exception. Repair creates only structural directories and known
  discovery links, preserves all user-owned seeded content, and reports launch blockers.
  A failure after directory publication preserves and reports that directory instead of
  deleting it, and routing changes require an Enso restart.
- `enso snapshot create --message <message> -- <paths...>` records one coherent local
  content change from explicit relative or absolute paths. Its closed allowlist covers
  instructions, canonical skills, reference docs, workspace knowledge, and recognized
  durable job files while protected/runtime and unknown paths fail closed. A persistent
  owner-only Enso lock serializes snapshots; dirty staging and pre-existing or unrelated
  native Git index locks are preserved and rejected. Snapshots are built and audited in a
  complete owner-only `.snapshot-index-<32-lowercase-hex>` inside the resolved Git
  directory, while an owner-only root `.snapshot.transaction.json` marker records the
  transaction and index checksum through atomic owner-only
  `.snapshot-transaction-<32-lowercase-hex>.tmp` writes. Verified descriptor reads,
  `hash-object -w --no-filters --stdin`, and
  `update-index --add --cacheinfo` preserve the reviewed bytes without worktree-attribute
  or clean-filter transformations. The alternate index is hard-linked to Git's
  `index.lock`, the old native checksum is rechecked, `HEAD` advances by atomic
  compare-and-swap, and that exact lock atomically replaces and fsyncs the native index
  without touching the worktree.
  Interrupted exact old/new ref/index states recover on the next call; unrelated locks
  and divergence are preserved and fail closed. No-diff requests are successful no-ops,
  effective partial-clone/promisor configuration is rejected, every Git child disables
  lazy fetching and transport protocols, and no snapshot operation contacts or changes a
  remote. Enso exposes no history, restore, reset, or delete subcommands.
- A portable `workspace` Agent Skill now guides workspace layout, focused instructions, global-versus-local skill placement, policy binding, validation, and safe retirement. Enso's bundled `docs`, `jobs`, `policy`, `slack`, `tables`, and `workspace` skills now use Agent Skills-compliant metadata with discovery-focused descriptions.
- Grok Build (xAI's `grok` CLI) is a fourth provider alongside `claude`, `codex`, and `agy`, selectable per workspace policy, Slack route, and job. Grok emits the Anthropic Messages wire format, so streaming event parsing and response formatting are shared with Claude; commands, sessions, reasoning effort (`--effort`, `low`–`xhigh` with no `max`), and instruction injection follow grok's own flags, with the prompt and the canonical shared instructions each riding as one attached argument (`--single=`, `--rules=`) so hyphen-leading content cannot be reparsed as a flag. A transient `Not signed in` auth failure — a lapsed OAuth token refreshing in the background — is retried once on the interactive path; job runs never retry.
- Restricted Grok policies launch from a revision-keyed staged `GROK_HOME` under `<policy_dir>/.runtime/grok-home`, generalizing the Codex staged-home machinery: owner-read-only policy `config.toml`, auth refreshed from the real Grok home each launch, an allowlisted child environment, and byte-level snapshot verification every launch. Because the CLI appends a `[marketplace]` stanza to its config after each run by replace-by-rename — which read-only staging cannot prevent — staging pre-seeds that stanza so the published bytes stay stable. `GROK_HOME`, `GROK_SANDBOX`, and `GROK_FOLDER_TRUST` are reserved from `env_passthrough`; these inputs remain part of the policy revision, whose current launch contract is v6 after the native instruction-discovery change below.
- `enso config check` gates every Grok policy binding dynamically. Grok loads zero permission rules from a wrong-shaped `[permission]` table with no error, no non-zero exit, and an empty `skipped` list, so the check materializes the stable checked bytes in a disposable `GROK_HOME`, runs `grok inspect --json` from the workspace under a separate scratch `HOME`, and requires the reported `permissions.loaded` count to equal the rules the policy declares. Loading fewer rules than declared means rules were silently dropped; loading more means rules reached the launch from outside the policy. The diagnostic reports the mismatch without echoing native source names or CLI output, and the check never creates canonical policy runtime state or reads user auth. Wrong-shaped and rule-less configs are also rejected statically.
- A Grok policy may not disable folder trust or stage its own `trusted_folders.toml`. Folder trust only ever loosens — with it off the CLI applies a workspace's own `.grok/config.toml` and vendor-compat settings — so an agent-writable workspace could otherwise grant itself rules, hooks, and MCP servers the policy never declared. A fresh staged home leaves the workspace untrusted, and both routes to undoing that are now closed. See [permissions.md](docs/specs/permissions.md#grok) for the staged-home contract, the silent fail-open risk, and the documented limit that home-scope vendor-compat sources (`~/.claude`, `~/.cursor`) are discovered relative to `$HOME` and are not excluded by a staged home.

### Changed

- Restricted policies now require an explicit `policy_dir`; the former implicit
  `~/.enso/policies/<name>` fallback is removed. Policy creation registers an already
  complete directory and never creates inactive scaffolds or permission content.
- The fresh-install root prompt and content-mutating bundled skills now require one
  scoped `enso snapshot create` after each coherent versionable content change, with
  explicit paths and no raw broad Git staging. `enso doc create` and `enso job create`
  remain unsnapshotted because they intentionally produce incomplete placeholders; the
  agent snapshots once after the follow-up edit is complete. Database, credential,
  upload, draft, policy, and runtime paths are explicitly excluded.
- Bundled shared and workspace `AGENTS.md` templates are transport-neutral and route detailed workflows into focused skills. Fresh setup copies them once; installed prompts and skills are user-owned, and startup, repair, and upgrades never replace or resurrect them. The expanded `slack` skill now teaches when to use ordinary rich Markdown, structured interactive replies, and requester-confirmed persistent-surface drafts without duplicating the runtime's versioned message contracts.
- Provider, model, and effort choices are now durable route settings instead of conversation state: one Slack DM or channel shares them across roots and threads, while each Telegram private chat keeps its own. `status` reports whether each effective value came from a route selection, policy/provider default, or CLI default, and `use default`, `model default`, and `effort default` clear the corresponding choice. The v3 state migration deliberately drops ambiguous v1/v2 conversation-scoped selections while preserving provider sessions, compact seeds, conversation activity, and job state; route settings no longer expire with `ENSO_SESSION_TTL_DAYS`.
- Slack `!` commands now follow the route's response triggers like ordinary messages. A responsive top level or already-joined thread accepts commands without a mention; mention-gated and unjoined threads remain gated, a bare `!` remains prompt text, and `chat_commands` still authorizes every command. Provider/model/effort commands are valid inside threads, but their replies make clear that the setting applies to the entire channel or DM.
- Slack credentials, transport options, and exact DM/channel routes now share the single `transports.slack` object. The legacy top-level `routes` key is rejected; move `routes.slack.account_id`, `channel_defaults`, `dms`, and `channels` beside the existing Slack credentials, remove `routes`, run `enso config check`, and restart Enso.
- Workspaces now select exactly one reusable policy. Slack routes and scheduled jobs select only a workspace and derive its provider, command, and native-policy authority; route/job policy overrides and the former top-level `access` catalog are rejected. Rename `access` to `policies`, add `policy` to every workspace, and remove `access` from every route and `JOB.md` before restarting Enso.
- The global top-level `working_dir` and `enso serve --working-dir` override are removed. Fresh installations always define workspace `default` at `~/.enso/workspaces/default`, bound to an unrestricted `admin` policy, and service definitions no longer set a process working directory; each provider subprocess receives only its resolved workspace as cwd.
- Telegram now requires `transports.telegram.workspace` and derives that workspace's policy exactly like Slack routes and jobs. Provider selection, command registration and callbacks, native launch, compaction, clearing, concurrency, session scope, and unique `uploads/<random-id>/` attachment directories all use the binding; configuration errors fail closed instead of falling back to a global unrestricted launch.
- Canonical shared Enso instructions live at `~/.enso/AGENTS.md` with
  `~/.enso/CLAUDE.md -> AGENTS.md`; each name-derived workspace has a focused local
  `AGENTS.md` and matching relative discovery links. Claude and Codex now discover both
  instruction and skill scopes natively from the exact `~/.enso` Git worktree, without a
  duplicate `--append-system-prompt-file` or `developer_instructions` override. Grok
  receives the freshly validated shared content once through `--rules`, and unrestricted
  Agy receives it once through Enso's prompt envelope. Every interactive attempt
  revalidates the current shared source, physical workspace, repository boundary, links,
  readable skill definitions, and duplicate names immediately before spawn. Jobs perform
  the same check before trusted prerun and again afterward. Invalid or partial discovery
  fails closed with no fallback delivery mode. The launch contract is now v6, rotating
  every policy revision for the changed invocation.
- Upgrading across these workspace-policy changes is a manual breaking migration with no
  `enso migrate` command or runtime compatibility path. Follow the
  [unified-policy guide](docs/migrations/unified-workspace-policies.md) for bindings and
  authority, then the [v1.3 workspace guide](docs/migrations/v1.3-managed-workspaces.md)
  to move content into name-derived physical roots, remove every legacy `path`, use
  `workspace repair` after relocation, validate, restart, and roll back from the operator
  backup if needed. `workspace create` deliberately refuses an already migrated
  destination.
- The web dashboard now makes the execution configuration explicit with workspace,
  reusable-policy, and exact Slack-route list/detail pages backed by the running config
  snapshot and cache-only Slack labels. Policy pages expose normalized provider checks
  but never native policy contents or secrets. The managed-versus-external workspace
  tier is removed: alternate and unsafe roots are invalid and their instruction content
  is never inspected or rendered, while shared and every valid canonical workspace-root
  `AGENTS.md` editor uses bounded, symlink-resistant, revision-checked atomic writes.
  Nested workspace instructions remain read-only.
- Slack channel history is pulled on demand instead of pushed into every new conversation. A top-level message used to arrive with the last 20 channel messages prepended, which in a channel where each request starts its own thread meant the roots of unrelated earlier threads — and the agent answered them. An unrestricted policy now receives, once per conversation, a `[Channel access]` block naming the channel and the `enso slack history` / `enso slack thread` commands for it, and reads history only when the request calls for it. Thread context is unchanged and still pushed. A restricted policy cannot be assumed to reach the network from its sandbox, so it keeps receiving the channel context it cannot fetch for itself.
- `enso slack history` and `enso slack thread` render what the transport's own injector did: display names instead of raw user IDs, inert `@name (ID)` mention text instead of live `<@U…>` tokens, forwarded-message bodies, and readable timestamps alongside the raw `ts` that `enso slack thread` takes. Channel lifecycle noise (joins, pins, archive events) is dropped unless `--all` is passed, `enso slack history` gains `--since` (`30m`, `24h`, `7d`) to bound the window, and `enso slack thread` gains `-n` to keep the root plus the most recent messages. A trimmed thread reports how many replies it dropped, so a partial read is never mistaken for the whole thread.

### Fixed

- Fresh `enso setup` now requires an explicit Slack or Telegram selection instead of accepting an empty transport, while a previously valid choice remains the default during reconfiguration.
- Slack's entity escaping is now decoded before message text reaches a model, in injected thread/channel context and in the `enso slack` reading commands alike. Slack stores a typed `<`, `>`, or `&` as `&lt;`, `&gt;`, and `&amp;`, so a command example someone posted arrived as `enso slack thread C0… &lt;ts&gt;`. Decoding runs before mention flattening, so a `<@U…>` it exposes is still flattened and raw mention syntax never reaches a prompt.

- `thread_mention_required: false` now follows threads Enso started itself. A top-level message Enso posts outside a dispatch — a job notification, `enso message send`, a surface confirmation — creates no conversation session, so replies under it were dropped until someone mentioned the bot once, even in a fully responsive channel. Enso's own thread roots now count as participation, read from the `parent_user_id` Slack stamps on every thread reply, so no extra API call is involved. Unchanged: `thread_mention_required: true` still gates own roots, threads rooted by anyone else still need a first mention, unrouted channels stay unrouted, and only human replies dispatch.
- A Slack conversation with no provider session memory yet now receives the full thread as context instead of only the messages since Enso last spoke. The narrow slice assumes Enso's own words are already in its session; before the first turn opens one, nothing carries them. In an Enso-rooted thread that left the root — the job report or `enso message send` the whole thread is about — permanently invisible to the model, which answered replies with no idea what they referred to. The full thread is sent once, on the turn that opens the session; later turns return to the narrow slice, so no history is re-sent.
- The untrusted-context header injected with Slack thread and channel history no longer describes every line as posted by someone else, since such a block can now carry Enso's own messages. The instruction to treat the block as data and never as instructions is unchanged and still covers every line, including job output relayed under an `[assistant]` label.

## [1.2.0] - 2026-08-13

This release adds per-channel Slack response triggers and two restricted-profile grants — environment passthrough and an exact Claude MCP server allowlist — alongside an internal restructuring of the runtime that leaves behavior unchanged.

### Added

- Restricted access profiles gain `env_passthrough`, a list of environment-variable names (names, never values) copied from the service environment into the otherwise fixed child environment for every policy-controlled provider. Names are validated (shape, no duplicates, launch-controlled and `ENSO_`-prefixed names reserved) and the key is invalid on an unrestricted profile. A configured-but-absent name warns at spawn; `enso config check` shows each name's resolvability against the invoking shell and `~/.enso/secrets/*.env`.
- Restricted Claude profiles gain an exact MCP server allowlist by convention: an optional `<policy_dir>/claude/mcp.json` beside `settings.json`, integrity-checked, hashed into `policy_revision`, and passed as `--mcp-config` with `--strict-mcp-config` retained so the profile sees those servers and never ambient ones. Absent means zero servers; present but unusable fails the turn closed. `enso config check` lists each profile's resolved server names and warns about `mcp__` permission rules matching no declared server, declared servers no allow rule references, and secret-shaped literals in `mcp.json`.
- Per-channel Slack response triggers: channel routes gain boolean `mention_required` and `thread_mention_required` (both default `true`, the original mention-gated behavior, so existing configs are unchanged), and an optional `routes.slack.channel_defaults` block supplies defaults that per-route keys override. `thread_mention_required: false` follows only threads Enso already participates in — participation comes from a prior authorized dispatch's conversation session, survives restarts, and lapses with session retention (`ENSO_SESSION_TTL_DAYS`) — so first contact in a pre-existing thread still needs a mention. `channel_defaults` is settings inheritance, not authorization: unrouted channels stay unrouted, and replies to channel messages always land in the message's thread.

### Changed

- Interactive agent turns and `/compact` now default to a 30-minute (1,800-second) `agent.timeout`, up from 15 minutes (900 seconds); `0` still disables it.
- The launch contract version is bumped to 3 (restricted Claude flags and child-environment construction changed), which rotates every `policy_revision`, including the unrestricted sentinel. Both new grants default to off; an install that adopts neither sees only the rotation.
- Inbound Slack mentions are flattened to inert text instead of being stripped wholesale, in the request and in injected thread/channel context: a leading bot mention is removed, any other bot mention becomes `@<bot name>`, and a mention of anyone else becomes `@<display name> (<ID>)` with an `@<ID>` fallback. The model now sees who a request is about, and raw `<@U…>` syntax never reaches it, so nothing the model echoes back can ping anyone. Profile display names are neutralized before interpolation (angle brackets, square brackets, and line breaks removed), so a crafted name can neither reintroduce live mention syntax nor forge the `[user …]` author labels on injected context.
- Machine-authored Slack posts never dispatch: Enso's own messages, other apps' posts (`bot_id`/`bot_profile`, which modern posts carry with no subtype), and Slackbot are dropped by both event paths before routing. Channel routes authorize human members, so bot content embedding a mention token cannot become an authorized request and two auto-responsive bots cannot reply to each other in a loop.
- A route whose binding fails at dispatch time reports the fixed configuration-error and audit-failure replies only to explicit contact (a mention, or any DM). Unaddressed traffic admitted by relaxed response triggers fails silently — audited routes still record the blocked turn — so a broken responsive channel is not spammed on every message.
- `!` commands always require explicit addressing: a mention in a channel (in every response-trigger mode) or any DM message. An unaddressed message starting with `!` in a responsive channel or followed thread is ordinary prompt text, never a command — a fixed rule, not a setting, so making a channel responsive never widens its command surface.
- Job scheduling and execution moved out of the runtime into a dedicated `enso.job_runner` module. Scheduler, prerun, and job-run log records now carry the `enso.job_runner` logger name instead of `enso.core` — log filters and alerts matching on logger name need updating. Scheduling, notification, locking, and run-recording behavior is unchanged.

## [1.1.0] - 2026-08-12

This release adds rich, structured Slack replies and requester-confirmed App Home and Canvas publication, while keeping persistent changes behind an exact preview and one-time human approval.

### Migration from 1.0.0

- Slack rich messages and persistent App Home/Canvas drafts now default to enabled when their config keys are absent. Existing Slack apps must apply the current bundled manifest, enable App Home and interactivity, grant `canvases:write` and `files:read`, reinstall or reauthorize when Slack requests new scope consent, and restart Enso. Set `transports.slack.persistent_surfaces` to `false` to disable only persistent drafts, or `transports.slack.rich_messages` to `false` to restore legacy text delivery and disable both rich paths. Running `enso setup` now refreshes `~/.enso/slack-app-manifest.yaml` even when credentials are left unchanged.

### Added

- Interactive Slack final answers render through Slack's standard Markdown block, including headings, links, fenced code, task lists, and Markdown tables. Long replies are split within Slack's 12,000-character limit without breaking fenced code or table rows, and known block-validation rejection falls back once to complete text.
- Agents can emit validated transport-neutral layouts for compact two-column fields, aligned/wrapped tables, pageable/sortable/filterable data tables, and native line, bar, area, or pie charts. Every layout carries a complete text fallback for accessibility, auditing, and non-Slack transports.
- Natural-language requests can prepare an App Home dashboard, standalone Canvas, or create-or-replace channel Canvas draft. Enso shows an exact inert preview with requester-bound Publish and Cancel controls before any persistent Slack API runs. Channel replacements identify and re-check the same Canvas and edit revision before replacing its complete body and title.

### Changed

- Slack rich replies and persistent surfaces are enabled by default, with independent explicit `false` rollback flags. CLI sends, file captions, status/error messages, direct notifications, and scheduled-job notifications remain text-only in this release.
- Native table validation follows Slack's published aggregate limits: 20,000 cell characters for data-table-only output and 10,000 when any simple table is present.
- The status message now shows `↳ Processing` from the moment it is posted, instead of only the model line, until the provider reports its first real status (e.g. `↳ Thinking`).

### Security

- Persistent-surface drafts expire after 15 minutes, are scoped to the authenticated account, exact route, requester, origin conversation, confirmation message, workspace, and access profile, and are consumed through an atomic one-time claim. Confirmation reauthorizes the route, creates required audit evidence before mutation, revalidates Canvas targets, serializes competing target updates, and never automatically retries an interrupted or ambiguous Slack mutation.

## [1.0.0] - 2026-08-10

This release replaces Enso's single-user Slack allowlist with an access-profile model. Every Slack DM and channel is bound by an exact route to one named workspace and one complete native-CLI policy, and restricted work runs under the provider CLI's own sandbox and permission system instead of Enso's bypass invocation. It is a breaking change; see Migration.

### Migration from 0.19.x

- Slack now requires `routes.slack`; a Slack transport with `transports.slack.allowed_users` or without routes is invalid. Map each authorized DM user (by exact Slack user ID) and channel (by exact channel ID) to a route that names a workspace and an access profile. There is no allowlist, wildcard, or default route, and routes are never synthesized.
- Move `unrestricted`, `policy_dir`, `providers`, `default_provider`, and `chat_commands` out of `workspaces` and into named `access` profiles; move directories into `workspaces`. `groups`, route `allow`, and route `context_from` are removed.
- Every job's `JOB.md` must add `workspace` and `access`; a job missing either is skipped.
- Telegram `allowed_users` must be exact numeric string IDs; the `allowed_user_ids` spelling and the `"*"` wildcard are removed, and only private chats are accepted.
- `enso policy check` is now `enso config check`. Run it, and `enso route explain slack <user> [channel]`, before restarting the service.

### Added

- Access profiles: either `unrestricted` (retaining the bypass invocation, for trusted administrative routes) or a `policy_dir` of native provider policies. Reusable `workspaces` (a provider cwd and shared-content root) and exact Slack DM/channel routes bind a workspace to a profile.
- Native non-bypass launch contracts. Claude launches with `--settings <policy> --permission-mode dontAsk --setting-sources project --strict-mcp-config`; Codex launches from a staged, revision-keyed, immutable `CODEX_HOME` with `--strict-config --skip-git-repo-check` (and `--ignore-rules` when the profile ships none). Policy-controlled children receive a minimal allowlisted environment holding only the active provider's credential.
- Scheduled jobs bind to a workspace and access profile and run under that native policy. The launch is proven constructible before the trusted host-side prerun runs, and no failure ever falls back to unrestricted execution.
- A metadata-only Slack delivery ledger for at-most-once dispatch across an event's message/mention twins and Slack retries, and an optional per-route turn-based audit trail (`audit`, with `audit.on_failure` and `audit.max_age_days`).
- New CLI: `enso config check` (static configuration and launch-plumbing validation), `enso route explain`, `enso audit tail`, and `enso audit export`.
- `enso setup` initializes `~/.enso` as a git repository with a protective `.gitignore`, and reports pre-existing tracked credential files instead of assuming the protection applies.
- Per-workspace concurrency limits shared across a workspace's routes and compaction.

### Changed

- Slack authorization is exact routes only: channel membership authorizes a channel route, and DM routes are keyed by exact user ID. (breaking)
- Jobs require explicit `workspace` and `access`. (breaking)
- Telegram requires exact numeric string IDs and rejects non-private chats. (breaking)
- `enso policy check` renamed to `enso config check`; it also validates job bindings and every route- and job-bound launch, and reports that it is a plumbing check, not proof that a policy is safe. (breaking)
- Dispatch threads an immutable execution context (cwd, launch, workspace, access) resolved at the provider spawn boundary; operational logs for routed work are metadata-only.

### Removed

- Legacy `transports.slack.allowed_users`, `groups`, route `allow`, route `context_from`, and permission fields inside `workspaces`. (breaking)
- Telegram `allowed_user_ids` and the `"*"` wildcard. (breaking)
- The Enso-curated skills allowlist; each CLI's own skill discovery applies, governed by native policy rather than by Enso.

### Fixed

- A bare `!` command no longer raises inside the Slack router, a crash that had left the delivery claim pending and silently dropped the message.
- Codex restricted launches pass `--skip-git-repo-check`; non-private Telegram chats are rejected; audit and ledger integrity and command-revocation gaps are closed; and crash-orphaned pending turns are reconciled at startup.

### Security

- Restricted routes and jobs execute under the provider CLI's native sandbox and permission system rather than `--dangerously-skip-permissions` or `--dangerously-bypass-approvals-and-sandbox`. A missing, unreadable, malformed, or structurally unsafe policy fails the turn closed; Enso never falls back to an unrestricted launch, another profile, another workspace, or the global `working_dir`.
- Enso's transport tokens, secret-manager token, database, and unrelated provider credentials are withheld from policy-controlled children. Policy files must be protected owner-only regular files outside every writable workspace, and overlapping or symlinked layouts are rejected. Native policy, not Enso, remains the authority on what a profile can read, write, run, or reach.

## [0.19.0] - 2026-08-05

### Added

- Telegram and Slack transport tokens, Slack Socket Mode app tokens, and the dashboard shared token can now use direct 1Password references in config. Enso resolves each reference through the machine-local `op_secret` helper at process startup or CLI use, keeps resolved values out of config and environment projections, preserves valid legacy literal keys, and fails closed on malformed or unavailable credentials. Reconfiguring a referenced transport through `enso setup` supplies replacement values over stdin, preserves the references, and never writes a plaintext fallback. Slack credential-pair updates prevalidate both old values and best-effort roll back an earlier write if the second fails.

### Fixed

- SQLite permission hardening no longer opens and closes live database or sidecar files, which could silently release every SQLite lock held by the process and disconnect active connections from their WAL files.
- `enso serve` reports unresolvable transport credentials as a clean one-line error and exits, matching the other commands, instead of surfacing a raw traceback.
- Slack setup insists on a Socket Mode app token at the prompt instead of accepting a blank value that either broke Socket Mode later or aborted a referenced credential update with a misleading 1Password error.
- Database lock contention no longer stalls the bot or web event loop or masquerades as empty data: Runs and Tables database-only pages return safe `503` busy/unavailable states, while dashboard and job pages preserve unaffected content.
- The `web` extra once again installs `python-multipart`, which Starlette requires to parse dashboard forms.

### Changed

- SQLite-backed run history and registered tables now use short-lived, operation-owned connections with bounded lock waits and explicit write transactions; async callers execute complete database operations in worker threads.
- Dashboard navigation, filters, pagination, and writes now use ordinary full-page links, forms, and `303` redirects.

### Removed

- The vendored HTMX runtime, fragment templates, request branches, loading indicators, and related client-side behavior.

## [0.18.1] - 2026-07-30

### Fixed

- Documentation caught up with what 0.18.0 actually shipped: `~/.enso/secrets/*.env` is now documented in the README and the `~/.enso/` layout (it had no coverage at all), the README gained a Reference Docs section for `enso doc`, the architecture implementation map gained `fsutil.py` and `docs.py`, the web spec's dashboard now lists the doc count it renders, and the docs spec's `AGENTS.md` delivery section no longer reads as an unimplemented proposal. Also corrects the `/effort` command description, the HTMX "latest stable" phrasing, the Run-now navigation description, and the documented release-commit convention

## [0.18.0] - 2026-07-30

### Added

- `enso serve` loads every `~/.enso/secrets/*.env` file into its own environment at startup, so background jobs — prerun scripts and the provider process alike — inherit credentials that a launchd or systemd environment would otherwise withhold. Files are read in filename order, `#` comments and a leading `export ` are tolerated, and a variable already present in the environment always wins
- Main-content HTMX navigation with full-page fallbacks, correct history restoration and POST URL transitions, persistent loading feedback, focused table/run fragments, and race-safe request replacement. Runs are now paginated at 50 records and rendered once across responsive layouts instead of duplicating every row
- Registered data tables in the existing `~/.enso/enso.db`: `enso table list/schema/register`, an explicitly catalogued and reserved-name-safe storage layer, owner-only database/WAL files, a bundled `tables` skill for consistent agent writes, and bounded read-only Tables pages in the dashboard with schema inspection, pagination, safe NULL/BLOB/truncation rendering, and responsive overflow containment
- Cron schedules are validated when a job is created (`enso job create` rejects malformed expressions), and the scheduler now skips — with a log warning — any job whose hand-edited schedule has become invalid instead of dying silently and stopping all scheduled jobs. Background scheduler/task deaths are now logged instead of vanishing
- A cross-process per-job run lock: the scheduler, `enso job run`, and the dashboard's "Run now" can no longer run the same job concurrently. `/update` also refuses to proceed while a dashboard- or CLI-triggered job is mid-run
- Agents now see documented `ENSO_ORIGIN_*` environment variables (transport, user id/name, channel id/name, thread) describing who triggered the current turn; previously they were exported but undocumented
- The four advanced tuning env vars (`ENSO_SESSION_TTL_DAYS`, `ENSO_JOB_CONCURRENCY`, `ENSO_PROCESS_TERMINATE_GRACE_SECS`, `ENSO_JOB_FAILURE_RENOTIFY_SECS`) are documented in the README and are snapshotted into the launchd/systemd service definition by `enso service install`, so they actually reach `enso serve` under a service manager
- Live activity is back in the status message, and now covers every provider: it shows the provider, model, and effort handling the request, the elapsed time, and what the agent is doing right now. Claude reports tool calls (preferring the model's own description over the raw arguments), Codex reports work items as they start, and Antigravity — whose headless mode prints only a final answer — reports activity read from its conversation trajectory. Providers with no progress on stdout can now supply it out of band via `poll_progress`, which the runner drains concurrently and treats as best-effort
- Interactive agent turns now have a provider-neutral timeout configured through `agent.timeout` (900 seconds by default; `0` disables it). A timed-out Claude, Codex, or Antigravity process tree is stopped, the user sees a concise terminal notice, and durable recovery context is injected only into that conversation's next turn so the agent can inspect partial work before continuing
- Google Antigravity CLI support through the registry-backed `agy` provider, including its current effort-qualified model catalog, background jobs, per-chat conversation resume, and automatic migration of existing configs. Enso captures Antigravity's generated conversation ID from a private per-run log and removes that log immediately afterward
- Existing job prerun scripts can now be viewed and edited below the prompt on the job detail page. Saves are atomic, preserve file permissions, and reject missing, symlinked, or out-of-directory paths
- Jobs and Enso-owned skills can now be deleted from their dashboard detail pages after confirmation. Entire owned directories are removed, external skills remain read-only, and deleted bundled skills stay deleted across service restarts
- The web dashboard now shows visible skill counts split into Enso-owned and system-wide tiers
- Job prerun failures and timeouts are now recorded in run history and notify through the configured Telegram or Slack destination. Identical alerts are suppressed for 24 hours, changed failures alert immediately, and healthy preruns send one recovery notification
- Codex model aliases `sol`, `terra`, and `luna` are available in Telegram/Slack model selection and background jobs. Enso translates them to the Codex CLI's `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` IDs while preserving older and custom configured models. These models require Codex CLI 0.144.0 or newer
- `/effort` and `!effort` now support Codex as well as Claude. Sol and Terra expose levels through `ultra`; Luna exposes levels through `max`. Per-chat overrides are passed to Codex as `model_reasoning_effort` while `default` falls back to the Codex CLI configuration
- `/update` (Telegram) / `!update` (Slack) deterministically updates Enso from the exact current commit on the stable `main` branch. It builds a wheel, validates it in an isolated environment, runs the upstream test suite, installs that same wheel, restarts the bot and dashboard services, and confirms health after restart. Already-current installs are left untouched, and editable development checkouts ahead of stable are never downgraded. Revision metadata is stored separately in `~/.enso/update.json`; the active model is never involved
- Edit a job's prompt (the `JOB.md` body) directly from the web dashboard, mirroring in-place skill editing
- Background jobs validate their `provider` and `model` against the configured providers before running: `enso job create` rejects unknown values upfront, and an existing job naming a retired provider or unknown model records a clear error run (and notifies) instead of failing obscurely at spawn time

### Fixed

- Status messages now tick every second for the first 30 seconds, then every five seconds, keeping short requests visibly alive without exhausting transport rate limits on long runs; each edit shows the latest agent activity, and a single failed edit no longer silences status for the rest of the request
- Provider errors that already begin with `Error:` are normalized before display, avoiding duplicated labels such as `Error: Error: Individual quota reached`
- The background message queue (`messages.json`) now serializes read-modify-write cycles across processes with a file lock, so a job-failure notice can no longer be silently lost when it races an agent's `enso message send`
- `/compact` now honors `agent.timeout` like interactive turns — a hung provider CLI no longer wedges the chat behind a held lock — and messages queued during compaction are dispatched right after it finishes instead of waiting for the next user message
- A first turn that fails before the provider CLI creates its session (bad `providers.claude.path`, auth failure, immediate exit) no longer permanently wedges the chat on `--resume` of a session that never existed; the pre-assigned session id is reverted so the next turn starts fresh
- Slack `channel_rename` events no longer reset cached channel fields (`is_member`, `is_private`, topic, member count) to defaults — minimal events only overwrite the fields they actually carry
- Slack job notifications are now truncated at Slack's 40,000-character limit instead of a hard-coded 4,096
- `enso web` no longer writes pruned session state back to `state.json` on startup, so a dashboard (re)start can no longer clobber the serve process's live state
- Deliberately removed Codex model aliases (`sol`/`terra`/`luna`) stay removed: the alias backfill now runs only for configs that predate the aliases, and user ordering is preserved
- The pre-0.12 bundled `slack_search.py` skill tool is now retired on upgrade (pristine-hash gated, like the tasks skill), including its installed `workspace/tools/` copy; customized copies are preserved with a warning
- The dashboard-service names the updater restarts and health-checks (`com.enso.web` / `enso-web.service`) are now documented — `enso service install` never creates them, but a user-created service under those names is managed by `/update`; a foreground `enso web` still needs a manual restart
- Antigravity conversations now run inside the project mapped to the Enso workspace instead of Antigravity's default scratch project, so workspace files, skills, and context resolve correctly. Fresh conversations reuse the project already catalogued for the working directory (including one created by interactive Antigravity use) and create one only when none exists; background jobs pin the same way. Chats whose stored conversation predates this fix stay pinned to scratch until `/clear` starts a fresh conversation
- Wide dashboard layouts now keep the capped main content aligned beside the sidebar
  instead of centering it farther away as the viewport grows
- Primary dashboard navigation now keeps the responsive application shell mounted and
  swaps only the stable main-content fragment, preventing the old whole-body HTMX swap
  from leaving the Jobs grid in its single-column layout
- Dashboard requests now reject unlisted Host headers; wildcard binds require explicit `web.allowed_hosts`. Writes require a process-scoped CSRF token, and responses send browser-hardening headers
- Job metadata now loads through PyYAML's string-preserving `BaseLoader` with a fallback for malformed legacy headers. Prompt and enable/disable edits preserve the raw frontmatter text
- Upgrades migrate legacy task run-retention settings, remove only pristine retired task artifacts, and preserve customized copies with a cleanup warning
- Known pristine bundled skills advance to the current version during upgrades, while customized files and symlinks remain untouched

### Changed

- Atomic file writes and pristine-file hashing are consolidated into one `enso.fsutil` module (seven near-identical copies removed); Claude hook settings are now written atomically too
- Telegram inline-keyboard provider/model taps route through the same shared command handlers as the text commands
- The runs page dropped its vestigial "Kind" filter (only `job` runs exist since the tasks system was removed)
- `/effort` help text no longer names only Claude/Codex — effort applies to the active provider, including Antigravity
- The bundled jobs skill, system prompt, and README now document the `agy` provider, the `catch_up`/`misfire_grace_seconds` job fields, and Telegram-only chat commands; the bundled slack skill and app manifest now agree that `enso slack search` uses the bot token's `search:read.public` scope
- Interactive progress is now provider-neutral: one transient message rotates playful status text with elapsed time and progressively backs off edits during long requests, while final answers no longer include provider, effort, usage, or duration prefixes. Provider-specific thinking and tool narration are no longer surfaced in chat
- Dashboard dropdowns now use consistent custom chevrons, spacing, hover and focus
  states, and dark-mode styling instead of browser-default select chrome
- The web dashboard now uses sidebar-aware breakpoints, readable mobile run cards, compact desktop grids, accessible form controls, simplified job detail views, and searchable deduplicated Skills. Long IDs and upload controls no longer widen phone layouts, and compiled Tailwind plus pinned HTMX assets are vendored for fast offline rendering without CDN requests
- Scheduled, CLI, and web job runs now share one prerun/provider pipeline. Exit `1` is reserved for intentional no-work; missing scripts, timeouts, and all other nonzero exits are failures. Manual runs report distinct outcomes and return a nonzero shell status for real failures
- The bundled agent-instruction template moved from `enso/system_prompt.md` to `enso/prompts/AGENTS.md`, making it easier to find and review separately from the code
- On setup, `AGENTS.md` is now the canonical instruction file in the workspace and `CLAUDE.md` is symlinked to it (previously reversed). Codex reads `AGENTS.md` natively
- Run-retention config moved from `tasks.runs_keep` / `tasks.runs_max_age_days` to a top-level `runs.keep` / `runs.max_age_days` block

### Removed

- The unused `python-multipart` dependency from the `web` extra and the dead `skills/**/*.py` package-data glob
- The alternate Claude runner integration — the `/kage` chat command, the `providers.claude` runner settings (`runner`, `job_runner`, `kage_path`, `kage_timeout`, `kage_restart`), and its provider adapter, tests, and documentation. Claude always runs through `claude -p`; upgrades strip the retired settings from `config.json` automatically
- The deprecated Gemini CLI provider and its obsolete configuration; Google model access now uses the Antigravity CLI provider
- Built-in one-off tasks system (the `enso task` CLI, the task-runner, and the tasks web UI) — use Todoist or jobs instead

## [0.17.0] - 2026-06-24

### Added

- Slack forwarded/shared messages are now read by the agent. When someone shares (forwards) another message to the bot — with or without an accompanying caption — Slack delivers the original content in the event's `attachments` array rather than `text`, so the bot previously saw only the caption (or nothing) and couldn't act on a request like "make a GitHub issue for this". The transport now renders each shared message (author, source channel, body, permalink) into the prompt, downloads any file the forwarded message carried (which hangs off the attachment, not the top-level `files`), and no longer drops a caption-less forward. This works on both the DM and channel @-mention paths

### Changed

- Forwarded/shared messages that appear in fetched Slack channel or thread history are now surfaced in the injected context instead of rendering as a blank line, so the agent sees shared content from earlier in the conversation too

## [0.16.0] - 2026-06-02

### Fixed

- Background jobs whose Claude turn ran longer than 60 seconds could hang until the wall-clock timeout and be force-killed even though the work had finished; job completion detection was reworked to be independent of turn duration

## [0.15.0] - 2026-05-31

### Added

- Configurable logging: log level and request/provider-flow debug visibility are now driven by config, with sane defaults backfilled for setup and existing installs (`logging_config.py`)

### Fixed

- Background-job process cleanup is hardened so a job's whole process tree is reliably torn down on completion, timeout, or cancel

## [0.14.0] - 2026-05-12

### Added

- `/compact` (Telegram) / `!compact` (Slack) command summarises the active session and reseeds a fresh one — keeps the thread alive but trims token usage between turns. It works for Claude and Codex by driving the existing provider pipeline (headless Claude has no native `/compact`). The summary is never shown to the user; it's injected into the next user prompt inside a `[Continuing from a previous session…]` envelope so the LLM sees prior context without paying for the full transcript. The seed is one-shot and persists across `enso` restarts so the contract holds if the user takes a break between `/compact` and their next message
- `/compact` posts an immediate ack ("Compacting context — this can take 10–30s…") before the summarisation pass starts, and refuses while a request is already running for that chat (asks the user to `!stop` or wait)

### Fixed

- Slack DMs with an image and caption are no longer silently dropped. The transport's `subtype` guard was rejecting every `message` event with a non-null subtype, including `subtype=file_share` (which is how Slack delivers an upload-with-caption). Replaced with an explicit denylist of lifecycle/noise subtypes so user-content subtypes fall through to the existing text/files handler
- Slack channel @-mentions with attached files now download the file and include it in the prompt. The `_handle_app_mention` path was ignoring `event["files"]`, so the bot saw the caption but never the image
- Slack canvas body @-mentions no longer trigger a duplicate dispatch with a failing `restricted_action` reply. Slack delivers both a `message`-with-`document_mention` and a real `app_mention` for the same user action; only the `app_mention` path can post into the canvas's auto-thread, so `document_mention` is in the ignore list for both handlers and the surviving `app_mention` handles the reply

### Changed

- Slack file downloads now run via `asyncio.to_thread`, so a slow `urlopen` no longer stalls the Bolt Socket Mode event loop while a large attachment fetches
- Slack Connect file placeholders (`file_access: "check_file_info"`) are hydrated with a `files.info` call before download, so cross-workspace shares no longer arrive as URL-less stubs and get silently skipped
- Slack attachment download filenames are now prefixed with the Slack file ID (`F123-image.png`). Two uploads with the same name in one message no longer collide on disk; a same-named upload from an earlier message no longer shadows the new one
- Slack download failures degrade gracefully: the agent receives `"User uploaded a file, but it could not be downloaded: <name>"` plus the caption, instead of the previous silent drop
- Multi-file Slack hydration runs in parallel via `asyncio.gather` rather than sequentially, so the cost of `files.info` lookups on a batch of placeholders no longer adds up

## [0.13.0] - 2026-04-17

### Added

- `/effort` (Telegram) / `!effort` (Slack) command to set Claude's reasoning effort level (`low`, `medium`, `high`, `xhigh`, `max`) per conversation and model. Uses Claude Code's `--effort` flag. The active level shows in the status line as `(Claude / xhigh / 25% / 30s)`
- Effort is stored per `(chat, provider, model)` and persisted in `state.json`; raw intent is kept and clamped to each model's supported range at read time, so switching between models preserves your picks. `/effort default` clears the per-chat override
- `ENSO_ORIGIN_*` environment variables injected into every provider subprocess — `ENSO_ORIGIN_TRANSPORT`, `ENSO_ORIGIN_CHANNEL`, `ENSO_ORIGIN_THREAD_TS`, `ENSO_ORIGIN_USER_ID`, `ENSO_ORIGIN_USER_NAME`, `ENSO_ORIGIN_CHANNEL_NAME`. The agent sees who triggered the current turn and where the reply should go
- `enso message send` / `enso message attach` auto-route back to the origin when invoked without `--to`. Priority: `--to` > `ENSO_ORIGIN_CHANNEL` > `notify_channel`. `thread_ts` propagates only when routing to origin (not on cross-channel overrides)
- Slack transport warms its directory cache (users + channels) on startup so origin-env name resolution works on the hot path without per-message API hits. Respects the cache's recency guard to avoid hammering the API on frequent restarts

### Fixed

- Slack `enso message attach` now includes `thread_ts` in `files.completeUploadExternal` so threaded uploads actually land in the thread. The misleading `completeUploadExternal: invalid_arguments` error from attaching with no destination is gone
- Slack `enso message send` now includes `thread_ts` in `chat.postMessage` so agent-initiated sends stay threaded

### Changed

- `TransportContext` gained `get_origin_env()`; transports populate it with transport-specific identifiers. Base class returns an empty dict so jobs and CLI-triggered runs fall through to `notify_channel` as before
- Telegram `enso message send` / `attach` honor `ENSO_ORIGIN_CHANNEL` — a bare send from inside an agent turn replies to the triggering user instead of broadcasting to all `allowed_users`. Broadcast still happens when no origin is set (e.g. jobs)

## [0.12.1] - 2026-04-16

### Fixed

- `pip install enso[slack]` now pulls in `aiohttp`, which is required by `slack_bolt`'s Socket Mode handler. Without it, a fresh `enso[slack]` install would crash on transport import with a misleading "slack-bolt and slack-sdk are required" error
- Slack transport import error now reports the actual missing module and chains the original traceback instead of swallowing it

## [0.12.0] - 2026-04-16

### Added

- Slack transport (Socket Mode) alongside Telegram — DMs, channel mentions, threaded replies, thread/channel context injection
- `enso slack` subcommand group — `lookup-user`, `lookup-channel`, `whois`, `open-dm`, `list`, `refresh`, `search`, `history`, `thread`. Backed by a local JSON cache at `~/.enso/cache/slack.json` with refresh-on-miss semantics (60-second rate guard)
- Slack transport now listens for `user_change`, `team_join`, and `channel_*` / `member_*` events and keeps the directory cache live in real time (requires the matching event subscriptions in the Slack app)
- Bundled Slack app manifest (`src/enso/slack_manifest.yaml`) pre-configures every scope and event Enso uses; `enso setup` copies it to `~/.enso/slack-app-manifest.yaml` for the "Create from manifest" flow
- `enso message attach` now supports Slack via the external file upload API (up to 1 GB)
- `--to` flag on `enso message send` and `enso message attach` for targeting a single destination (user ID on Telegram; channel/DM/user ID on Slack)

### Changed

- `enso setup` warns when Slack is chosen without a `notify_channel`, since background sends, job alerts, and autocompact hooks all need one
- Setup test-send on Slack now uses the same `notify_channel`-only resolution as the runtime (previously fell back to the first allowed user, which hid the gotcha)
- The bundled `slack_search` skill is now a lightweight `SKILL.md` that points the agent at the `enso slack` CLI — no more per-workspace Python tool script
- Slack `notify` (and CLI sends) never auto-broadcast — a destination must come from `--to` or `notify_channel`
- Telegram `notify` now honors the `destination` kwarg for single-target sends; omitting it still broadcasts to all `allowed_users`

## [0.11.1] - 2026-04-15

### Changed

- Completed audit of `feat/multi-transport` branch for v0.12.0 readiness (#2)

## [0.11.0] - 2026-04-09

### Added

- Telegram reply support — reply to any message in the chat and the quoted context is included in the prompt, with the bot's response visually threaded back
- Auto-compact notification — when Claude auto-compacts context, a Telegram message is sent immediately so you know why the next response is slow. Hooks are installed automatically on setup.
- Message queue — messages sent while a request is running are queued (up to 5) and auto-dispatched when the current request finishes. `/queue` to view/remove items, `/stop` clears the queue.
- Context window usage percentage in Telegram response prefix — `(Claude / 11% / 23s)`
- Launchd plist now snapshots API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) so provider CLIs work under launchd's minimal environment
- 15-minute hard timeout for background jobs (previously could hang indefinitely)

### Fixed

- Token usage percentage now uses last assistant turn's per-turn counts instead of cumulative modelUsage totals (was over-reporting by 3-4x)
- Collapse excessive blank lines (3+) in formatted output

## [0.10.0] - 2026-04-02

### Added

- Inline keyboard buttons for `/use`, `/model`, and `/clear` — tap to select instead of typing
- `/model` now shows available models as tappable buttons (merged `/models` into `/model`)
- `/clear` shows "Clear current" / "Clear all" buttons instead of requiring `/clear all`
- Active provider/model marked with ● in button lists

### Removed

- `/models` command (folded into `/model`)

### Fixed

- Progressive backoff on status message edits to avoid Telegram flood control errors

## [0.9.1] - 2026-03-31

### Changed

- Prerun exit code convention: `exit 1` = no work (silent skip at DEBUG), `exit 2+` = real error (WARNING with stderr)
- "Running job" log now fires after the gate passes — idle gated jobs produce zero INFO output
- Runner captures stderr from prerun scripts for error diagnostics

### Fixed

- harbour-poll: API failures (curl errors, empty claims) now exit 2 instead of being silently swallowed as "no work"
- youtube-playlist-summaries: playlist fetch failure now exit 2 instead of exit 1

## [0.9.0] - 2026-03-31

### Added

- `enso message attach <file> [caption]` — send files (images, video, audio, documents) to Telegram
  - Auto-selects Telegram API method based on file extension (sendPhoto, sendVideo, sendAudio, sendDocument)
  - Captions rendered as HTML with markdown conversion
- Both `send` and `attach` now queue a background message so the agent retains context of what was sent

### Changed

- Merged `enso message send` and `enso message notify` into a single `send` command — sends to Telegram immediately and queues as background context
- Removed `enso message notify` (redundant)
- Updated system prompt and bundled skills to document `attach` and simplified `send`

## [0.8.0] - 2026-03-31

### Added

- Native Telegram slash commands with autocomplete menu (replaces `!` prefix commands)
- Markdown → HTML rendering for agent responses, notifications, and CLI messages
  - Bold, italic, underline, strikethrough, inline code, code blocks, links, headers, blockquotes
  - Word-boundary guards prevent false positives on snake_case and math expressions
  - Fallback to plain text if HTML parsing fails
- Typing indicator while agents work (refreshed every 4s)
- Thinking/narration surfaced in status updates (Claude thinking blocks, Codex agent messages)
- `/logs` command to view recent log entries from Telegram

### Changed

- Status prefix format simplified to `(Provider / Xs)` — model name removed, parens instead of brackets
- Response prefix on its own line so markdown headings render correctly
- Session ID `new:` prefix stripped on spawn instead of on result, preventing "already in use" errors

### Fixed

- `clear_session` now only deletes the specific session file Enso owns, not all sessions in the project directory

## [0.7.0] - 2026-03-30

### Changed

- Renamed project from Overlord to Enso
- Package name: `enso`
- CLI command: `enso`
- Config directory: `~/.enso/`
- Service identifiers: `com.enso.agent` (launchd), `enso.service` (systemd)

## [0.6.0] - 2026-03-25

### Added

- `enso message notify` command — sends directly to Telegram (real-time, not queued)
- Documented installation directory structure, symlink strategy, and agent compatibility in README

### Changed

- Telegram is now a required dependency — install with `pip install -e .` (no extras needed)
- Setup goes straight to Telegram configuration (no transport picker)
- Jobs only notify on failure — successful jobs handle their own messaging via `enso message notify`
- Rewrote system prompt and `jobs` skill to document `message notify` and silent-by-default behavior
- Response text splitting applies provider prefix before splitting (consistent across chunks)

### Removed

- `[telegram]` optional extra — Telegram is always included
- Transport discovery machinery (`available_transports`, `get_transport_class`)
- Slack/Discord placeholders

## [0.5.0] - 2026-03-19

### Added

- Pluggable transport system — Telegram is now an optional dependency
- Built-in job scheduler with croniter (60s tick, no launchd/systemd per job)
- Background message queue (`enso message send/list/clear`) with auto-injection into next conversation
- CLI subcommands: `enso job`, `enso message`, `enso service`
- Service management: `enso service status/install/uninstall/start/stop/restart/logs`
- Bundled `jobs` skill (agentskills.io spec) with format reference and examples
- Skills auto-discovered via `.claude/skills` and `.agents/skills` symlinks
- Session isolation — Enso-managed UUIDs prevent cross-session bleed with local CLI usage
- 50 pytest tests

### Changed

- Config shape: `transport`/`transports` dict replaces hardcoded `telegram` key
- Job runner is now Python (replaces bash scripts: runner.sh, notify.sh, install.sh)
- `enso job create` scaffolds with `enabled: false`, agent edits JOB.md directly
- Providers add `--` before prompts to prevent content parsed as CLI flags
- Stderr surfaced as error events (no more silent "(No response)")
- System prompt simplified — jobs detail moved to skill

### Removed

- Platform-specific per-job scheduling (launchd plists, systemd units for individual jobs)
- `BACKGROUND_MESSAGES.md` file approach (replaced by `messages.json`)
- Hardcoded Telegram dependency in core

## [0.4.0] - 2026-03-05

### Changed

- Renamed project from Operator to Overlord
- Package name: `overlord-ai`
- CLI command: `overlord`
- Config directory: `~/.overlord/`
- Service identifiers: `com.overlord.agent` (launchd), `overlord.service` (systemd)

## [0.3.0] - 2026-02-12

### Added

- Auto-install system prompt files to the working directory on serve
- Bundled system prompt as package data so it ships with the package
- Existing user-customized prompt files are never overwritten

### Removed

- Symlinked instruction files from the repo root (canonical source is now bundled in the package)

## [0.2.0] - 2026-02-12

### Added

- Telegram file upload support (documents, photos, audio, voice, video)
- Files downloaded to `{working_dir}/uploads/` and passed to the active agent
- Caption text included as context alongside the file path

## [0.1.1] - 2026-02-12

### Fixed

- Lowered Python requirement from 3.12 to 3.10

## [0.1.0] - 2026-02-12

Initial public release.

### Added

- Interactive setup wizard (`enso setup`) with provider detection, Telegram bot onboarding, and working directory configuration
- Telegram transport with live status updates as agents work
- Support for Claude Code and Codex agents
- Chat commands for switching providers, models, stopping tasks, and managing sessions
- Background service installation for macOS (launchd) and Linux (systemd)
- Platform-aware setup summary with service management commands
