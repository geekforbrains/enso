# Task 04: Slack Message Formatting

**Status:** Deferred (raw markdown pass-through for now — Slack's mrkdwn is close enough)
**Priority:** Medium
**Depends on:** Task 02

## Description

Enso's Telegram transport converts Markdown to Telegram HTML via `formatting.py:md_to_html()`. Slack uses its own "mrkdwn" format which is close to standard Markdown but has differences.

## Slack mrkdwn vs Standard Markdown

| Feature | Standard Markdown | Slack mrkdwn |
|---|---|---|
| Bold | `**text**` | `*text*` |
| Italic | `*text*` or `_text_` | `_text_` |
| Strikethrough | `~~text~~` | `~text~` |
| Code | `` `code` `` | `` `code` `` (same) |
| Code block | ` ```lang\ncode``` ` | ` ```code``` ` (no language highlight) |
| Links | `[text](url)` | `<url|text>` |
| Blockquote | `> text` | `> text` (same) |
| Headers | `# text` | No equivalent — use `*text*` (bold) |

## Solution

Create `md_to_slack()` in `src/enso/formatting.py` (or a new `src/enso/transports/slack_formatting.py`):

1. Convert `**bold**` → `*bold*`
2. Convert `[text](url)` → `<url|text>`
3. Convert `# Header` → `*Header*`
4. Strip language tags from code blocks (Slack ignores them)
5. Leave everything else as-is (code, inline code, blockquotes, strikethrough all work)

## Alternative

Since AI agent CLI output is mostly plain text with some Markdown, we could start by just passing raw text through and only fix issues as they arise. Slack renders plain text fine, and code blocks work identically.

**Recommendation**: Start with a minimal pass-through approach. Add `md_to_slack()` only if formatting issues become noticeable.

## Acceptance Criteria

- [ ] Agent responses render readably in Slack
- [ ] Code blocks display correctly
- [ ] No broken formatting from unconverted Markdown
