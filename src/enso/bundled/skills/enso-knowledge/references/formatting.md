# Knowledge formatting

This is the common writing style for shared and workspace knowledge. Change it when the
user asks for different conventions, and keep `../scripts/lint.py` consistent with the
mechanical rules below. Metadata and link validity are checked by `enso knowledge audit`.

## Writing

- Use descriptive filenames: the filename supplies the viewer's title. An H1 is optional;
  do not repeat the title just to fill a template. Start with useful content.
- Use sections only when the content needs them. Prefer short paragraphs and lists; use
  tables for genuine comparisons. No mandatory summary, tags, status, or source section.
- Keep current information easy to find. Preserve useful history with dates; distinguish
  confirmed facts from proposals. Cite sources close to the relevant claims.
- Link to the owning note instead of copying its contents. Use a path when names collide.
- Keep code examples in fenced code blocks, with a language when known. Avoid raw HTML
  when ordinary Markdown expresses the same thing.
- Retain meaningful layout in imported notes. Do not rephrase prose or restructure folders
  as a side effect of metadata adoption or mechanical formatting.

## Mechanical checks

The default checker reports `path:line: rule: explanation`, without changing files:

| Rule | Convention |
| --- | --- |
| `line-endings` | UTF-8 text with LF line endings |
| `final-newline` | A nonempty note ends in a newline |
| `trailing-whitespace` | No trailing spaces or tabs outside code; exactly two spaces after text are allowed for an intentional Markdown line break |
| `blank-lines` | At most one consecutive blank line outside frontmatter and code |
| `heading-spacing` | A blank line before and after a Markdown heading, except at the start or end of the body |

Frontmatter, fenced code, and indented code are excluded from body style checks. Long lines
are allowed. No heading hierarchy, H1, filename casing, prose structure, or mandatory
section is enforced. The checker walks visible Markdown files under the supplied paths,
skips hidden directories and symbolic links, and exits 0 for clean files, 1 for findings,
or 2 for an input/read error. Run it on changed notes; importing a collection need not
rewrite every historical note to satisfy a new style.

```bash
python3 "$ENSO_HOME/skills/enso-knowledge/scripts/lint.py" "$ENSO_HOME/shared/knowledge/Reference"
```
