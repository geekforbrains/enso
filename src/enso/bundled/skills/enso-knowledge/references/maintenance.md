# Moves, links, and imports

Use `enso knowledge move REF DEST` for renames or moves. Add `--to-workspace NAME` or
`--to-shared` to change roots. It preserves identity and repairs links that the move would
break. Read its report and audit affected scopes afterwards. Broad reorganization or
deletion needs to be within the user's request; preserve attachments and unrelated files.

Unqualified links resolve inside the source note's scope. A bare title must be unique;
otherwise use a folder path or explicit scope. Supported examples:

```markdown
[[Topic]]
[[Projects/Topic#Decision|Decision]]
[Decision](../Projects/Topic.md#decision)
[[shared:Projects/Topic]]
[[workspace:research:Projects/Topic]]
```

Check destinations and headings before repairing links. A clean audit does not establish
factual correctness or external website availability.

For a requested import, copy the source and preserve its original. Use a repeatable script
for a collection, retaining folders and attachments. Run `enso knowledge adopt PATH` on
copied Markdown, selecting `--workspace NAME` when needed. Adoption preserves valid IDs and
known dates, retaining unsupported metadata in the body. Review its report and audit for
unresolved links and import exceptions before claiming completion. Restructuring an import
is a separate decision from normalizing its metadata.
