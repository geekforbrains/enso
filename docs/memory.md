# Memory

Markdown memory, its CLI, live Slack/Telegram capture, bounded harvesting, the workspace
job, explicit removal, and `enso-memory` skill are implemented for 0.2.0.
These features are not available in 0.1.x.

## Purpose and ownership

Memory records dated conversations and experiences as ordinary Markdown under
`$ENSO_HOME/workspaces/<name>/memory/`. There is no shared or home-level memory root.
The containing workspace owns the notes, and the Markdown is the source of truth for
maintained memory. Parsed caches or search indexes must be rebuildable from those files.
[The web viewer](web.md#memory) provides a read-only audit of current memories and stored
capture evidence, with separate **Memories** and **Captures** tabs. Its workspace and
transport filters, paginated lists, source links, and receipt-based processing labels do
not change the Markdown or processing state. Live capture is best effort: the viewer does
not show complete chat history, truncated text cannot be recovered there, and attachment
references are not extracted contents.
[Knowledge](knowledge.md#knowledge-and-memory-in-020) holds current maintained facts and
reference material in shared or workspace roots.

People sharing a workspace share its maintained memory. Their DM conversations and live
provider sessions remain distinct; a shared workspace does not make one person's session
available to another conversation. Separate workspace names organize context and ownership
within the same [trusted installation](concepts.md#installation-trust-model).

## The note format

One note records one coherent event, discussion, or decision, with enough context to explain
what happened and what remained uncertain. It is not a transcript or an automatically
updated statement of current truth. A long discussion can produce several notes, and a
useful event can draw on several captures from the same workspace.

For example, `workspaces/team/memory/2026/09/16/launch-date-proposal.md` can contain:

```markdown
---
schema: enso.memory/v1
id: 4d6d560a-21ef-49fa-a4db-7640b8dcab89
occurred: "2026-09-16T18:20:00Z"
sources: [1201, 1202]
created: "2026-09-16T18:30:00Z"
updated: "2026-09-16T18:30:00Z"
---

The team proposed September 25 for launch. Capture 1201 records the proposal;
1202 says testing still needs to finish. No launch date was confirmed.
```

| Field | Rule | Purpose |
| --- | --- | --- |
| `schema` | Required; exactly `enso.memory/v1`. | Identify the supported memory format. |
| `id` | Required; a unique UUID, preserved through edits and moves. | Give the note a stable identity independent of its path. |
| `occurred` | Required; a quoted ISO 8601 timestamp with timezone, a quoted `YYYY-MM-DD` date, or `null` when unknown. | Record when the event happened at the precision actually known. |
| `sources` | Required; a list of distinct positive integer capture IDs, or `[]` for a manual/imported memory with no captures. | Identify the source records in the home database. |
| `created` | Set on new notes; optional on imports. | Record original document creation time, separate from the event. |
| `updated` | Set on new notes and substantive edits; optional on imports. | Record the latest document update, including link repairs or corrections. |

These are the only permitted frontmatter fields. There are no title, tag, scope, workspace,
or access fields: the filename supplies the title and the path supplies ownership. Source
IDs refer to captures, not note IDs or platform message IDs. Harvested notes must cite
their inputs, and every cited capture must belong to the containing workspace. Capture
references remain useful after a provider session is reset. Manual memories use body text
for other provenance, such as a person's recollection or an imported document; they never
invent capture IDs. Missing or workspace-mismatched source IDs are reported rather than
silently removed or reassigned. Imported provenance from another installation belongs in
the body unless its captures are deliberately mapped to records in this installation;
matching numbers alone do not establish that relationship.

Memory reads, audits, and managed updates check source existence and workspace ownership
in SQLite without creating a database. Missing sources and an unreadable database are
reported; files remain readable and unchanged, but managed updates refuse invalid sources.
Do not erase source IDs to bypass a finding. Manual notes with `sources: []` need no database.

### Occurrence, placement, and corrections

`occurred` records the event, not when harvesting ran or a file was copied. For a discussion
segment it is the time of the first source relevant to that note; describe a meaningful
time span in the body. Enso writes known instants in UTC ending in `Z`, and places them under
`memory/YYYY/MM/DD/` using that UTC date. For example, `2026-09-16T23:30:00-07:00` is
`2026-09-17T06:30:00Z` and belongs under `2026/09/17/`, even on a Vancouver machine.

A date-only value such as `"2026-09-16"` goes under `2026/09/16/`; it is not converted to
midnight or shifted by a timezone. Unknown occurrence uses `occurred: null` and `undated/`.
For example, an imported recollection with no dates has `occurred: null`, `sources: []`,
and no `created` or `updated`. Neither import time nor filesystem modification time proves
an event or original document date. Known document timestamps use ISO 8601 with a timezone;
Enso writes UTC, and `updated` cannot precede `created`.

Later corrections preserve the note's ID and distinguish historical belief from later
knowledge. For example, a September 20 edit can add a dated correction saying the team
confirmed September 28, retaining the September 16 proposal and its sources; `updated`
changes, while `occurred` remains September 16. Include the correction's capture IDs when
available. A distinct later decision can instead have its own linked note. An actual error
in the recorded occurrence date may be corrected with an explanation and matching folder
placement; do not redate history merely because the file changed. A move without a content
change preserves document timestamps.

### Editing, imports, and links

Humans can edit Markdown directly, and files copied into the memory root are discovered
without a database registration. Valid IDs and known dates are preserved. Malformed or
unsupported metadata, unknown fields, duplicate IDs, missing sources, and mismatched date
folders are reported with their paths; reading or auditing never rewrites them. Imported
text remains inspectable by path, but invalid metadata is not treated as valid managed
memory. Repair it deliberately while preserving original context and unknown dates. Copies
that duplicate an ID are ambiguous; lookup by ID and managed writes must not choose a winner.

CLI updates require the exact-byte SHA256 from the last read (`--expected-hash`), preserve
identity and occurrence unless explicitly corrected, and publish atomically. A stale hash
refuses the edit. CLI writers update `updated` for substantive changes; direct editors
maintain it themselves when known. File modification time can invalidate a search cache,
but never silently changes metadata. Harvesting and crash recovery preserve human edits
instead of overwriting them. Safe reads and writes refuse symlinks and escaping paths.
For example, if a person corrects the proposal after an agent reads it, the agent's saved
hash fails the update check and the person's correction remains untouched.

Use ordinary relative Markdown links, including optional heading fragments. For example,
the September 16 proposal can link to the September 20 decision with display text
"Confirmed date" and the relative target `../20/launch-date-confirmed.md`.
Resolution is from the source file, without wiki-name guessing or a search through other
workspaces. Broken links are reported. A direct filesystem move preserves the note's ID
but does not repair relative links; maintain those links explicitly when relocating files.

### Manual maintenance

Use `enso memory create NAME.md --occurred VALUE --file FILE` to record an event in the
selected workspace. The occurrence is required: a known date, a timestamp with timezone,
or the explicit word `unknown`. Enso chooses the date folder and writes `sources: []` for
manual notes. Describe recollections, documents, and uncertainty in the body.

`show --json` supplies the exact-byte hash for `update --expected-hash`. An ordinary update
replaces the body, preserving the ID, occurrence, sources, and any known creation date;
an unchanged body leaves the entire file unchanged. Include the original historical context
and dated correction in the replacement body. Unknown imported creation dates stay absent.
Read and audit do not adopt or normalize imported files.

`update --occurred VALUE` explicitly corrects the event time, including `unknown`. The file
must already be in the matching date folder. If the correction changes its day, deliberately
relocate the file first, maintain affected relative links, then update its occurrence with
an explanation in the body. The file's hash survives a content-preserving move. A placement
finding remains visible until the correction succeeds; the CLI never silently moves a file
or rewrites related historical notes. Other invalid metadata still needs deliberate repair.

`list` and `search` return newest known events first, with unknown or invalid occurrence
last, and path order to break ties. They inspect one workspace; `--workspace` overrides
`ENSO_WORKSPACE`. There is no shared memory selector, directory inference, or fallback
workspace. IDs are checked across workspace memory roots for duplicates, but lookup and
writes require the selected owner. The reader and writer limit each note to 2 MiB.

`audit` checks metadata, duplicate identity, date placement, links, and heading targets;
findings include their root/path and never rewrite files. Hidden/system entries and
symlinks are excluded, and an occupied or linked memory root is reported. CLI writers use
a home `.memory.lock` and atomic publication shared with knowledge's filesystem primitives;
the two note formats retain their own metadata rules. Listing and search rebuild their view
from the files, reusing only an in-memory parse cache invalidated by file identity, size,
modification time, and change time. [CLI](cli.md#memory) owns exact signatures and results.
`enso doctor` summarizes the same checks across workspace memory roots, including capture
source validity and unsupported home-level memory. It cannot prove that a recollection is
true or that the selected workspace is the most useful owner.

## Conversation capture

**Implemented for Slack and Telegram in 0.2.0.**

Enso captures eligible live human messages it receives while running in a bound
conversation, after resolving its workspace. This includes messages that do not address
Enso. Capture is best effort for live traffic: it does not fetch transport history, backfill
outages, or promise to record messages Enso never received. [Connections](connections.md#access-in-020)
owns admission; mention/thread settings affect replies rather than capture eligibility.

Captures are operational records in the single home SQLite database. Every transport
supplies the same normalized record: stable identity, transport, workspace, conversation,
thread, sender ID and display name, timestamp, text, attachment references, and kind.
Slack retains the original event text, including platform mention markup; Telegram retains
the original text or caption. A display name unavailable at admission is empty, never an
invented identity. Replies retain the authenticated bot's ID and name when available.
Timestamp and message identity come from the authenticated transport.

| Kind | Meaning |
| --- | --- |
| `addressed` | An eligible human message accepted for handling; capture is attempted before provider execution, including while queued |
| `ambient` | An eligible human message Enso only observed |
| `reply` | Enso's final response, stored separately and linked to the addressed message, with generation and delivery outcomes |

Capture preserves the original user request, not the assembled provider prompt. Unbound
input, bot-originated messages, chat commands, pairing messages, and canned unbound notices
are excluded. Injected history, background context, system guidance, tool calls/results,
progress messages, and internal formatting-repair turns are not new conversation captures.
Attachments remain workspace-owned references. Recovery never reruns a provider or resends
a message merely to complete a missing capture.

### Storage and processing state

The fresh 0.2.0 schema reserves `_enso_captures` for permanent history. An integer capture
ID identifies each input or reply; `(transport, channel, message_id)` deduplicates human
messages independently of workspace bindings. A reply has one unique addressed parent,
with the same workspace, conversation, channel, and thread. The first human text, sender,
time, and attachment metadata remain unchanged; normal preparation can update attachment
download status and workspace-relative paths.

Addressed inputs remain pending until their reply handling finishes. Outcomes distinguish
completed, failed, cancelled, timed-out, dropped, empty, and interrupted handling. The
addressed record describes handling; its reply describes generation. A stop after generation
can therefore retain a completed answer with cancelled handling and partial delivery. A reply
checkpoints generation separately from delivery; `finalized` says whether its delivery
attempt has ended. Each delivery part records its character range, known message ID, and
sending/sent/failed/uncertain state. Ranges refer to the full representation and may extend
beyond a truncated stored prefix. Service-start recovery can mark unfinished inputs
interrupted and in-flight sends uncertain; it never starts work or sends messages.
A request captured before a process interruption may have no reply record at all.

Storage queries require a workspace, optionally a conversation, and return stable capture-ID
order (receipt chronology) after a cursor. Source timestamps preserve event time separately.
A page is limited to 100 captures and 128 KiB of source text, stopping before either budget
is exceeded. Reads expose truncation explicitly alongside the stored prefix.

Processing reservations live in `_enso_memory_receipts` and `_enso_memory_inputs`; each
capture can belong to only one receipt. A pending receipt holds the selected source IDs and
publication plan for recovery. Empty outputs explicitly mean no useful memory. Completion
is recorded only after the caller verifies durable publication; SQLite does not prove a
Markdown file was written. `_enso_memory_progress` advances independently per workspace,
only past contiguous completed inputs. These are processing records, never an authoritative
copy of maintained memory. The harvesting CLI publishes and reconciles the planned files
before completing these receipts.

### Text, rich replies, and attachments

Each capture stores at most **64 KiB (65,536 bytes) of UTF-8 text**. Oversized text retains
the longest prefix that fits without cutting a UTF-8 character. A separate `truncated`
flag records the loss; readers and harvesting input display an explicit truncation notice
outside the captured text. The storage limit does not shorten the live request given to
the provider or the response delivered to the user. For example, a 70 KiB ASCII request
keeps its first 65,536 bytes with `truncated: true`; a claim beyond that prefix is unavailable
to memory, not evidence that the claim was absent from the original message.

Final replies use readable Markdown representing the user-facing content. Ordinary text
stays text; transport-sized pieces are separated by blank lines, with per-piece ranges and
acknowledgments. Empty splitting artifacts are not sent. Rich Markdown blocks remain
Markdown, tables retain their headings and cells, and charts become a caption and a Markdown
table of their labels and values. Preserve block order. Do not store the `enso-message` JSON envelope or formatting-repair dialogue
as the answer. If the transport sends a text fallback instead, capture that fallback as
the delivered representation. For example, a chart titled "Signups" with Monday 4 and
Tuesday 7 becomes that title and those two rows, not a claim that an image was retained.
The same text limit applies after this conversion.

Attachments are references, not embedded bytes or extracted document text. Record the
transport attachment ID, supplied filename, media type and size when known, and a
workspace-relative upload path only when normal turn preparation successfully stored it.
Mark whether the file was downloaded, was not downloaded, or failed to download. Metadata
and filenames are untrusted data and cannot determine an arbitrary local read or download.
Do not retain authenticated download URLs or credentials as durable references.

An attachment-only message has empty text and its attachment references. For example, an
ambient photo can have its transport ID and filename with no local path; capture alone
does not download it. An addressed photo can gain a reference to its ordinary workspace
upload when preparation succeeds. Harvesting must not claim to know an attachment's
contents from its filename. Missing or failed downloads remain visible as missing context.

### Replies, edits, and retries

There is one logical reply linked to its addressed capture, even when delivery splits it
into several transport messages. Record generation outcome separately from delivery:
completed generation is not proof the user received it. Preserve known delivery message
IDs and which parts were sent; distinguish complete, partial, failed, unattempted, and
uncertain delivery. For example, if the first of two pieces sends and the second fails,
record partial delivery, not a successful complete reply. The stored representation may
include unsent content; only acknowledged parts establish what the user received. A timeout
with an unknown send result remains uncertain, never assumed sent or retried just for capture.

Stopped, failed, timed-out, or dropped turns retain their known outcome. With no final
user-facing response, record that no final reply was produced or delivered rather than
turning streamed progress, provider diagnostics, or an unfinished internal answer into a
completed response. If partial answer text actually reached the user, preserve that text
with the incomplete outcome. An empty response is not an invented successful answer.

Deduplicate by the authenticated transport's original message identity. A retry reuses
the original capture and workspace; it cannot create another memory input or reassign it
after a binding change. Keep the first received eligible message snapshot. Later edit and
deletion events neither revise captures nor dispatch fresh turns for this feature, and
are not captured separately. A human correction sent as a new message is a new capture.
For example, redelivering message 1201's transport event does not add a second capture;
a new message explaining a changed date does.

Standalone outbound sends, including `enso message send`, job notifications, and Heartbeat
messages, stay in the existing outbox and are not `reply` captures. Only the final response
to an addressed human message is a reply. Outbox context injected into a later turn is not
recaptured, so a harvesting job cannot recursively learn its own output as a conversation.

### Capture storage failures

Capture is best effort even after admission. If the addressed write fails, let normal
conversation handling continue; do not reject, drop, or delay the turn indefinitely just
to record memory. Log the storage failure once with operational identifiers, without
message bodies, attachment contents, credentials, or provider prompt text. An ambient
write failure is logged without producing a chat response. Reply or delivery-state write
failures likewise do not undo successful user-facing work.

For example, if storage fills before a request is recorded, Enso can still answer, but
that exchange may be missing from memory. Do not promise it was remembered, invent a
source ID or a missing parent capture, or rerun a provider or resend a message to fill the
gap. Any persisted incomplete state remains explicit for later diagnostics and recovery.

## Recall and maintenance

When a user asks about earlier conversations, decisions, promises, or follow-ups, the agent
uses the memory CLI and the `enso-memory` skill. Search the selected
workspace first, inspect relevant notes and their source context, and broaden deliberately
if needed. A fresh provider session can find maintained memory through that lookup.

Workspace harvesting turns bounded batches of captures into useful memory without mixing
another workspace's captures or fetching chat history. It records only notable events: a
decision, a commitment, a change, or an outcome, from addressed or ambient discussion alike.
A batch with nothing notable produces no note, which is an ordinary result rather than a
failure. Captured text is untrusted evidence: notes distinguish human statements, observed
discussion, agent suggestions, attempted work, and confirmed outcomes. Notes retain
references to their source captures. Durable processing receipts record handled inputs so
retries can recover safely.

For example, a bound team's channel discusses a possible launch date without mentioning
Enso. Those live messages are ambient captures. A later memory can record the proposal;
when asked what the team discussed, Enso finds that note and does not present the proposal
as a confirmed launch date. A confirmed current date can be deliberately promoted into the
owning knowledge note, retaining its source context.

Common requests do not require storage terminology:

- “What did we say about the launch?” searches memory and checks the relevant sources,
  even in a fresh chat session.
- “Remember that we agreed to Friday” records the dated agreement, keeping a proposal
  distinct from a confirmed decision; a lasting current date can also update knowledge.
- “That recollection is wrong” adds a correction with its context instead of changing
  the original occurrence to today.
- “Remove that memory” previews the specific note and requires confirmation before
  removing it; source captures and completed processing receipts remain, so a later sweep
  does not recreate it. See [Retention and removal](#retention-and-removal).

### Harvesting schedule and bounds

Every workspace has an enabled job named **`enso-memory`**, referenced as `<workspace>:enso-memory`,
scheduled hourly by default (`0 * * * *`, at the top of the hour).
[Jobs](jobs.md#workspace-memory-job-in-020) owns installation and job customization.
The prerun skips provider execution when no new
captures need processing. This is a polling cadence, not a promise that every capture is
summarized within an hour: service downtime, execution time, and backlog affect latency.

Each run handles at most **100 captures or 128 KiB (131,072 bytes) of stored source text**,
stopping before either limit would be exceeded. Attachment-only captures count toward
the 100-capture limit even when text is empty. Process captures in stable capture-ID order in
one workspace, preserving conversation/thread boundaries as separate segments. A segment
that exceeds the remaining budget continues in a later run; do not merge unrelated
conversations or mark an omitted capture handled. Mark segment boundaries and incomplete
context in harvesting input. Provider follow-ups share the run's input budget, not a new
allowance each time.

The job stores its selected IDs once for the run in `_enso_memory_batches`, keeping one
current batch per workspace. Result checks require that exact batch, including on bounded
provider follow-ups. They validate the final JSON and create the notes outside the provider
turn. Invalid results request correction in the same session; failed or timed-out provider
turns leave the inputs unprocessed. Job output, follow-up feedback, and notifications are
operational records rather than new source conversations.

For example, 130 short captures need at least two runs: at most 100 now and the remainder
later. Two full-size text captures exhaust the text budget even though the count is below
100. A workspace with no new captures makes no provider call. Ordinary sweeps do not fetch
older transport history or revisit already-processed captures to fill a segment.

Record a validated note result or an explicit no-memory result durably before considering
an input processed. Durable receipts and the per-workspace processing position reconcile
interrupted note publication; advance that position only past contiguous handled inputs.
A retry must not duplicate notes, lose unprocessed inputs, or overwrite a human correction.
Markdown publication and database receipts are separate writes and require recovery; they
are not one atomic transaction.

### Validating and publishing a pass

`enso memory batch` first reconciles pending publication receipts, then returns one JSON
batch for the selected workspace. It does not call a provider. Finished captures are offered
in ID order after the processing position, excluding inputs already reserved by receipts.
Selection stops at an unfinished capture so a live reply or attachment checkpoint cannot
change the evidence being summarized. Once that turn finishes, or service startup marks it
interrupted, it can be processed. A missing database means no captures, not a reason to
create a database during a read.

The batch contains its identity, source IDs, and separate consecutive conversation/thread
segments. Every segment warns that surrounding context may be missing. A boundary can split
a conversation across passes; the CLI never fetches older history to fill it. Sources show
sender, original time, kind, attachments, truncation, handling/generation outcomes, and
delivery parts. Use `enso memory source ID` in the same workspace to inspect a retained
source when recalling a note.

Submit a JSON result with `enso memory publish --file FILE`. Every selected capture must
be cited by at least one note or explicitly listed in `no_memory`, never both. An empty
`notes` list is valid only when `no_memory` explicitly covers all inputs. Each generated
note supplies a filename, Markdown body, and selected source IDs. Enso sets the schema,
stable UUID, timestamps, and UTC occurrence folder from the earliest cited source. It
appends the UUID to the filename to avoid colliding with existing notes. The model cannot
choose another workspace, supply arbitrary metadata, or cite an input outside the batch.

Validation finishes before any inputs are reserved or files published. The receipt retains
the exact planned Markdown, note identities, paths, and hashes before publication begins.
The shared memory writer lock serializes publication and recovery against managed manual
edits. Scheduled memory hooks retry brief collisions for up to five seconds when workspace
jobs start together; other concurrent writers get a retry error. A stale result cannot
reserve its captures again. Before-publication crashes replay the saved plan;
after-publication crashes reconcile existing identities before completing the receipt.
A completed receipt is never replayed, even if someone later deletes its notes. No recovery
path reruns a provider or sends a message.

If someone edits a created note before its receipt completes, recovery keeps the edited
file and finishes the receipt when the note is valid, has a nonempty body, and retains its
unique identity and all original source IDs. Additional valid correction sources are
allowed; they are not marked processed merely by appearing in the correction. A valid move
within the workspace is found by identity. Missing original references, invalid metadata,
duplicate identities, and occupied destinations stop recovery with a reported conflict;
the files and pending receipt remain available for deliberate repair, without overwriting
the person's work.

The validator checks structure and provenance, not the truth of prose. Summaries must still
attribute statements, preserve uncertainty, distinguish proposed actions from attempts and
confirmed outcomes, and treat embedded instructions as untrusted conversation text.
Generating an answer does not prove it was delivered; an attachment filename does not
establish its contents. Inspect the sources when a conclusion matters.

## Retention and removal

Captures remain history; 0.2.0 has no capture-deletion operation. The explicit
[`enso memory remove`](cli.md#memory) operation selects exactly one note by UUID or exact
path in the selected workspace. With no `--yes`, it previews the note's ID, path, and source
references and deletes nothing. `--yes` prints the selected-note report before deleting
that note. It accepts no globs, filters, multiple references, or bulk-removal switch.
Workspace selection follows `ENSO_WORKSPACE` with optional `--workspace`; duplicate or
otherwise ambiguous identity is an error, not permission to select a file arbitrarily.

Removal rechecks the reported file's exact-byte hash under the memory writer lock and
refuses intervening edits, links, or unsafe paths. A note still named by an unfinished
publication receipt cannot be removed: run `enso memory batch` in its workspace to finish
recovery, resolve any reported conflict, then preview it again. This prevents an unfinished
receipt from recreating a file the removal command just deleted. Removal itself does not
change captures, receipts, or the processing position, and a manual note needs no database.

For example, preview the team's launch proposal, inspect the reported note, then repeat
the command with `--yes` as shown in the CLI example. Preserve its source captures,
processing receipts, and processing position. Ordinary later sweeps must not recreate it
from already-processed captures, including after a direct human deletion of the Markdown
file. New messages about the same subject may produce new memories; removal is not a ban
on remembering that subject. Links to a removed note may become broken and are reported
by validation rather than causing deletion of other notes.

Session reset clears only the provider session; it never deletes captures or memory.
Removing a memory does not automatically remove facts deliberately promoted into knowledge.
Deletion of an active note also does not erase copies retained in backups or Git history.
