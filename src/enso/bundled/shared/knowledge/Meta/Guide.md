This guide owns the collection's organization and writing conventions. Read it before
creating, moving, or substantially editing notes. Change these preferences when the user
asks; the folders and templates are a starting point, not application requirements.

## Filing

Start at [[shared:Meta/Index|Index]] or search for the subject. Keep one owning note and
link to it from other contexts. New notes belong in shared knowledge unless the user gives
a different destination; update retained workspace notes where they already live.

| Folder | Purpose |
| --- | --- |
| `!Inbox/` | Captures awaiting review or a clear destination |
| `Meta/` | This guide, the collection index, and optional templates |
| `Memory/` | Daily logs of notable chat events, written by the `enso-memory` job |
| `People/` | People and useful relationship context |
| `Projects/` | Finite efforts with a defined outcome |
| `Areas/` | Ongoing responsibilities, operations, and standards |
| `Topics/` | Reusable concepts, reference material, and research |
| `Ideas/` | Explorations that have not become committed projects |
| `Archive/` | Inactive material worth retaining |

Choose the main subject, not the source medium. A reusable concept discovered during a
project belongs in `Topics/`; the project links to it. Create subfolders only when useful
and preserve established destinations. A job's agreed filing rule takes precedence.

During inbox review, update or link an existing owner when possible, retain sources, and
move reviewed captures to their subject home. Linking a capture does not itself complete
review. Preserve historical evidence when archiving; age alone does not make a reference
obsolete. Work product stays in its repository, app, or workspace `work/` until worth
retaining as knowledge.

## Navigation and note size

Keep one useful subject per note. Split when a section deserves independent retrieval or
updates, without fragmenting every paragraph. Link related notes in context so the reader
knows why to follow them; there is no link quota or fixed word count.

Add subject notes or hubs to [[shared:Meta/Index|Index]] as the collection grows. Use hubs
only when they help readers choose among several notes. Aim to reach maintained notes
within three outgoing links: Index → subject hub → optional sub-index → note. Chronological
series can use a dated index. Inbox captures, memory logs, templates, and archives can be
found by folder or search instead; they should not be the only route to maintained notes.

## Writing and sources

- Use descriptive sentence-case filenames with spaces, preserving proper names and
  acronyms. Dated logs can use `YYYY-MM-DD.md`. Avoid filesystem-unsafe characters.
- The filename supplies the title. Start with useful content instead of repeating an H1.
  Add `##` sections and `###` subsections only when useful.
- Distinguish confirmed facts, proposals, the source's claims, and the agent's synthesis.
  Keep source URLs or dates beside claims that need evidence; say when freshness was checked.
- Use contextual wikilinks or relative Markdown links. Qualify ambiguous names with a path
  or scope, such as `[[shared:Meta/Index]]`. Keep attachments beside their supporting notes.
- Use UTF-8, LF endings, a final newline, blank lines around headings and code fences, and
  at most one consecutive blank line outside code. Avoid trailing whitespace except two
  spaces for an intentional Markdown line break. Preserve meaningful imported layout.

The `enso-memory` job keeps `Memory/YYYY-MM-DD.md`: one bullet per notable chat decision,
correction, commitment, or result, in the machine's local time, such as
`- **03:17 PM**: One factual sentence.` Treat these logs as dated records. Correct a wrong
entry in place, but keep maintained knowledge in its owning note. Disable the job in its
`JOB.md` to stop the logs.

## Templates and maintenance

Use [[shared:Meta/Templates/Person|Person]] or [[shared:Meta/Templates/Project|Project]] when
helpful. Copy only the sample body, remove prompts and empty sections, and let the knowledge
CLI generate fresh metadata. Small notes need no template.

Use `enso-knowledge` for reads, safe writes, imports, and link-preserving moves. Run its
Markdown checker on changed notes and the core audit for their scope. Keep the checker
aligned when changing mechanical style preferences. Neither check verifies factual truth.
Core metadata, permanent identity, and safe writes remain application rules.
