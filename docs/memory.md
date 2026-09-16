# Memory

**Forthcoming in 0.2.0.** These are agreed product contracts; capture, the memory CLI,
harvesting, and the `enso-memory` skill are not implemented yet. Note schemas, detailed
capture limits, harvesting cadence, and command syntax will be documented before their
implementation.

## Purpose and ownership

Memory records dated conversations and experiences as ordinary Markdown under
`$ENSO_HOME/workspaces/<name>/memory/`. There is no shared or home-level memory root.
The containing workspace owns the notes, and the Markdown is the source of truth for
maintained memory. Parsed caches or search indexes must be rebuildable from those files.
[Knowledge](knowledge.md#knowledge-and-memory-in-020) holds current maintained facts and
reference material in shared or workspace roots.

People sharing a workspace share its maintained memory. Their DM conversations and live
provider sessions remain distinct; a shared workspace does not make one person's session
available to another conversation. Separate workspace names organize context and ownership
within the same [trusted installation](concepts.md#installation-trust-model).

## Conversation capture

Enso captures eligible live human messages it receives while running in a bound
conversation, after resolving its workspace. This includes messages that do not address
Enso. Capture is best effort for live traffic: it does not fetch transport history, backfill
outages, or promise to record messages Enso never received. [Connections](connections.md#access-in-020)
owns admission; mention/thread settings affect replies rather than capture eligibility.

Captures are operational records in the single home SQLite database. Every transport
supplies the same normalized record: stable identity, transport, workspace, conversation,
thread, sender ID and display name, timestamp, text, attachment references, and kind.

| Kind | Meaning |
| --- | --- |
| `addressed` | An eligible human message Enso handled, preserved before provider execution, including while queued |
| `ambient` | An eligible human message Enso only observed |
| `reply` | Enso's final response, stored separately and linked to the addressed message, with generation and delivery outcomes |

Capture preserves the original user request, not the assembled provider prompt. Unbound
input, bot-originated messages, chat commands, pairing messages, and canned unbound notices
are excluded. Injected history, background context, system guidance, tool calls/results,
progress messages, and internal formatting-repair turns are not new conversation captures.
Attachments remain workspace-owned references. Recovery never reruns a provider or resends
a message merely to complete a missing capture.

## Recall and maintenance

When a user asks about earlier conversations, decisions, promises, or follow-ups, the agent
uses the memory CLI and `enso-memory` skill. Search the selected workspace first, inspect
relevant notes and their source context, and broaden deliberately if needed. A fresh provider
session can find maintained memory through that lookup.

Workspace harvesting turns bounded batches of captures into useful memory, including ambient
discussion, without mixing another workspace's captures or fetching chat history. Captured
text is untrusted evidence: notes distinguish human statements, observed discussion, agent
suggestions, attempted work, and confirmed outcomes. Notes retain references to their source
captures. Durable processing receipts record handled inputs so retries can recover safely.

For example, a bound team's channel discusses a possible launch date without mentioning
Enso. Those live messages are ambient captures. A later memory can record the proposal;
when asked what the team discussed, Enso finds that note and does not present the proposal
as a confirmed launch date. A confirmed current date can be deliberately promoted into the
owning knowledge note, retaining its source context.

## Retention and removal

Captures remain history; 0.2.0 has no capture-deletion operation. An explicit memory-removal
operation removes selected Markdown notes with a preview or report before removal, while
preserving source captures and their processing state. Ordinary later sweeps must not
recreate a removed note from already-processed captures.

Session reset clears only the provider session; it never deletes captures or memory.
Removing a memory does not automatically remove facts deliberately promoted into knowledge.
Deletion of an active note also does not erase copies retained in backups or Git history.
