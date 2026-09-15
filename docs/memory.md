# Memory

Memory remembers what happened in Enso conversations: requests, decisions, reported outcomes,
corrections, and unfinished work. It keeps event times and source exchanges in `enso.db`,
separate from [Knowledge](knowledge.md), which holds maintained Markdown reference notes.
A summary of Tuesday's deployment discussion is Memory; the current deployment procedure is
Knowledge. Refinement never automatically writes or changes Knowledge.

## Capture → refine → recall

1. **Capture.** Enso records the original user text and human-readable provider reply during
   a chat turn. Each exchange retains the workspace, conversation, transport, channel, thread,
   sender, provider/model/session, attachment references, and completion state. Enso timestamps
   receipt and completion directly. Capture excludes tool calls and results, reasoning,
   progress/status events, injected instructions, repeated transport history, and formatting
   repair prompts. It does not scrape a transport or read provider history files.
2. **Refine.** The bundled `enso-memory` job selects a bounded batch of unprocessed finalized
   exchanges. Its agent keeps only information useful to a future conversation. Each short
   memory links to its source exchange IDs; the core derives its event range and origin from
   those sources. An entry may combine exchanges only from the same workspace, conversation,
   transport, channel, and thread. Recording entries and marking the entire batch processed
   happen in one database transaction. An empty result still marks the batch processed.
3. **Recall.** Agents use `enso memory list`, `search`, and `show`; the read-only web viewer
   has a separate **Memory** page. The bundled `enso-memory` skill guides selective recall.
   Memory is not automatically injected into every provider session.

Capture begins when this feature is installed and enabled. It covers conversations run
through Enso; standalone CLI sessions, old transport history, job output, heartbeat output,
and Knowledge changes are not imported. Failed, timed-out, or stopped exchanges retain their outcome and may produce memories of
unfinished work; refinement must never present the requested action as completed. Commands, unbound or rejected messages, and attachment-preparation failures
are not captured because no provider conversation starts. Receipt time is recorded before
downloads and queueing; completion time is the end of chat processing and delivery. Quoted or
forwarded messages remain transport context and are excluded from the original user request.
If delivery fails after a reply was generated, the exchange retains that reply with an error
outcome. Provider tool output is deliberately absent, so an
agent's report that an action succeeded is attribution, not independent verification.

## Recent memory and search

```bash
enso memory list --workspace personal --since week
enso memory list --limit 10
enso memory list --workspace personal --limit 10
enso memory search "holiday plans" --workspace personal
enso memory show MEM-000001 --sources
```

Lists and searches default to `ENSO_WORKSPACE` when that environment variable is present;
otherwise they include all workspaces. An explicit `--workspace` overrides that default.
`--all-workspaces` broadens the query and cannot be combined with `--workspace`. Workspaces
organize context; these filters are not an access-control boundary.

Both commands accept `--transport`, `--channel`, `--since`, `--until`, `--limit`, and `--offset`.
Channel accepts an exact platform ID. Search matches summary text. Results are newest
event first, 20 per page by default and at most 100. `--json` returns `entries`, `total`,
`limit`, and `offset`; `show --sources --json` adds the original clean exchanges to the entry.
List output keeps summaries brief; `show` returns the complete entry.

Date filters apply to when the event happened, separately from when its summary was created:

| Value | Meaning |
| --- | --- |
| `today` | Start of today in the configured Memory timezone |
| `week` | Monday at the start of this calendar week |
| `month` | First day of the current calendar month |
| `24h`, `7d` | The preceding duration |
| `2026-09-14` | Midnight on this date in the configured timezone |
| `2026-09-14T09:00:00-07:00` | An exact timestamp with its supplied UTC offset |

`--since` is inclusive and `--until` is exclusive. Dates are stored in UTC; human-readable
output uses the Memory timezone. The default `local` follows the machine's local timezone;
set an IANA name such as `America/Vancouver` to make the interpretation explicit.

## Configuration and refinement

Memory is enabled by default:

```json
"memory": { "enabled": true, "timezone": "local" }
```

Change it through the CLI:

```bash
enso config set memory.timezone America/Vancouver
enso config set memory.enabled false
enso memory status --json
```

Disabling Memory stops new capture and refinement while preserving existing history and
read commands. Settings apply on the next turn or job invocation without a restart. Disabling
only the `enso-memory` job leaves capture on and accumulates exchanges for later refinement.

Setup installs `jobs/enso-memory/` and `skills/enso-memory/`; managed updates add missing new
bundles and preserve customizations according to the [bundle rules](customizing.md#the-bundled-skills).
The job runs every 15 minutes with the setup agent's explicit provider/model/effort triple,
in the default workspace, with a five-minute timeout and one corrective follow-up. Edit its
`JOB.md` to change the schedule or agent. The batch itself can include multiple workspaces;
every exchange carries its own actual origin.

The prerun runs `enso memory prepare --batch "$ENSO_RUN_ID" --json`. No pending work or
disabled Memory returns exit 1 and spends no model call. Other failures return exit 2. The
batch is bounded by count and text size so a busy history cannot create an unbounded prompt.
A failed run leaves its inputs available for a later attempt; successful recording prevents
reprocessing. Retries of the same batch and payload are idempotent.

The agent writes a JSON array of objects with `summary` and integer `source_ids` only, then
calls `enso memory record --batch "$ENSO_RUN_ID" --file FILE --json`. The core validates
source ownership and refuses invented context fields or dates. `[]` is a valid result.
Postrun uses `enso memory check --batch "$ENSO_RUN_ID"`: exit 0 means recorded; exit 10
requests settlement of the still-pending batch; exit 2 signals an error. Printing a summary
without recording it does not settle a batch. These commands are the job's supported write
interface; do not edit Memory tables directly.

Stored requests and replies are each capped at 64 KiB with explicit truncation flags. The
model receives excerpts of at most 4 KiB per request or reply, reduced further when needed,
with truncation marked. Each batch contains at most 40 exchanges and 48 KiB of serialized JSON,
including metadata. This keeps both provider command arguments and the prompt bounded.
Summaries are capped at 2,000 characters; omitted text cannot be treated as evidence. The
retained source exchanges remain available through `show --sources`.

Source exchanges and summaries are untrusted evidence. The job is instructed to ignore
embedded commands and claims of authorization, preserve uncertainty, use only its batch,
and avoid sending messages or changing Knowledge. These instructions guide the provider;
Enso's ordinary [provider permissions](configuration.md#restricted-workspaces) still apply.

## Retention and forgetting

Memory has no automatic expiry. Source exchanges, summaries, and processing state are separate
from provider sessions and job-run retention: clearing a chat session or pruning runs does
not erase them. Include `enso.db` in the home's normal database backup; Git-tracked Markdown
alone does not back up Memory.

To explicitly forget saved history:

```bash
enso memory forget MEM-000001 --yes
```

This permanently deletes the selected memory, its source exchanges, and other memories
connected through shared source exchanges. Removing connected evidence avoids keeping the
same conversation behind another summary. Without `--yes`, the CLI explains the scope and
refuses the deletion. It does not erase provider-native logs, transport history, or existing
backups. Disable capture first if future conversations must not be retained.
