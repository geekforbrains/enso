# Formatting fallback

Use this only when the collection has no `Meta/Guide.md`. Preserve established filing and
the user's existing preferences. New notes default to shared knowledge.

- Use descriptive filenames; the filename supplies the title, so an H1 is optional.
- Start with useful content. Add sections, lists, and tables only when needed.
- Keep current facts distinct from proposals and history, with sources beside claims.
- Link to the owning note instead of duplicating it; qualify ambiguous names.
- Preserve meaningful layout and wording in imported or historical notes.

The editable `scripts/lint.py` checks UTF-8/LF text, final newlines, trailing whitespace,
blank lines around headings, and at most one consecutive blank line. Frontmatter and code
are excluded; two trailing spaces after text allow a Markdown line break. It imposes no
title casing, line-length limit, mandatory sections, or H1. It walks visible Markdown,
skips symlinks, and exits 0 when clean, 1 for style findings, or 2 for input errors.

When the user changes mechanical preferences, update this fallback and the checker together.
Metadata and links remain the core audit's responsibility.
