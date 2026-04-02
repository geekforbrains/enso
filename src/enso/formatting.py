"""Convert standard Markdown to platform-specific formats."""

from __future__ import annotations

import re
from html import escape

# Pre-escape patterns (matched before HTML escaping)
_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")

# Post-escape patterns (matched after HTML escaping, order matters)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_BOLD_STARS = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDER = re.compile(r"__(.+?)__")
_ITALIC_STAR = re.compile(r"(?<!\*)\*(\S(?:[^*]*\S)?)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![_\w])_(\S(?:[^_]*\S)?)_(?![_\w])")
_STRIKE_DOUBLE = re.compile(r"~~(.+?)~~")
_STRIKE_SINGLE = re.compile(r"(?<![~\w])~(\S(?:[^~]*\S)?)~(?![~\w])")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BLOCKQUOTE = re.compile(r"(^&gt; .+(?:\n&gt; .+)*)", re.MULTILINE)


def md_to_html(text: str) -> str:
    """Best-effort Markdown → Telegram HTML conversion.

    Stashes code blocks and inline code first so their contents aren't
    touched, escapes HTML entities, then applies formatting patterns.
    Falls back gracefully — partially converted text is better than
    a parse error.
    """
    # Stash code blocks
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        lang = m.group(1)
        code = escape(m.group(2).strip())
        idx = len(blocks)
        if lang:
            blocks.append(f'<pre><code class="language-{escape(lang)}">{code}</code></pre>')
        else:
            blocks.append(f"<pre>{code}</pre>")
        return f"\x00B{idx}\x00"

    text = _CODE_BLOCK.sub(_stash_block, text)

    # Stash inline code
    inlines: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        idx = len(inlines)
        inlines.append(f"<code>{escape(m.group(1))}</code>")
        return f"\x00I{idx}\x00"

    text = _INLINE_CODE.sub(_stash_inline, text)

    # Escape HTML entities in remaining text
    text = escape(text)

    # Formatting (order matters: bold before italic, double before single)
    text = _HEADER.sub(r"<b>\1</b>", text)
    text = _BOLD_STARS.sub(r"<b>\1</b>", text)
    text = _BOLD_UNDER.sub(r"<u>\1</u>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER.sub(r"<i>\1</i>", text)
    text = _STRIKE_DOUBLE.sub(r"<s>\1</s>", text)
    text = _STRIKE_SINGLE.sub(r"<s>\1</s>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)

    # Blockquotes (> is already escaped to &gt;)
    def _fmt_blockquote(m: re.Match) -> str:
        lines = [line.removeprefix("&gt; ") for line in m.group(0).split("\n")]
        return "<blockquote>" + "\n".join(lines) + "</blockquote>"

    text = _BLOCKQUOTE.sub(_fmt_blockquote, text)

    # Restore stashed code
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", block)
    for i, inline in enumerate(inlines):
        text = text.replace(f"\x00I{i}\x00", inline)

    return text


# ---------------------------------------------------------------------------
# Markdown → Slack mrkdwn
# ---------------------------------------------------------------------------

# Patterns for Slack conversion (matched in order)
_SLACK_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_SLACK_INLINE_CODE = re.compile(r"`([^`]+)`")
_SLACK_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_SLACK_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_SLACK_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SLACK_STRIKE = re.compile(r"~~(.+?)~~")


def md_to_slack(text: str) -> str:
    """Best-effort standard Markdown → Slack mrkdwn conversion.

    Stashes code blocks and inline code first so their contents aren't
    touched, then converts formatting patterns. Slack mrkdwn differences:
      - Bold: *text* (not **text**)
      - Italic: _text_ (same)
      - Strike: ~text~ (not ~~text~~)
      - Links: <url|text> (not [text](url))
      - Headers: *Header* (bold, no header syntax)
      - Code blocks: ```code``` (no language tag rendering)
    """
    # Stash code blocks (Slack ignores language tags but ```...``` works)
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        code = m.group(2).strip()
        idx = len(blocks)
        blocks.append(f"```\n{code}\n```")
        return f"\x00B{idx}\x00"

    text = _SLACK_CODE_BLOCK.sub(_stash_block, text)

    # Stash inline code (identical syntax, just protect from other transforms)
    inlines: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        idx = len(inlines)
        inlines.append(f"`{m.group(1)}`")
        return f"\x00I{idx}\x00"

    text = _SLACK_INLINE_CODE.sub(_stash_inline, text)

    # Convert formatting (order matters: bold before italic)
    text = _SLACK_BOLD.sub(r"*\1*", text)          # **bold** → *bold*
    text = _SLACK_HEADER.sub(r"*\1*", text)         # # Header → *Header*
    text = _SLACK_STRIKE.sub(r"~\1~", text)         # ~~strike~~ → ~strike~
    text = _SLACK_LINK.sub(r"<\2|\1>", text)        # [text](url) → <url|text>
    # _italic_ and >blockquotes work the same in both — no conversion needed

    # Restore stashed code
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", block)
    for i, inline in enumerate(inlines):
        text = text.replace(f"\x00I{i}\x00", inline)

    return text
