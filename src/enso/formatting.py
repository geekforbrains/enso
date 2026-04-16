"""Convert standard Markdown to Telegram-safe HTML."""

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

    # Collapse excessive blank lines (3+ newlines → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Restore stashed code
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", block)
    for i, inline in enumerate(inlines):
        text = text.replace(f"\x00I{i}\x00", inline)

    return text


# ---------------------------------------------------------------------------
# Slack mrkdwn
# ---------------------------------------------------------------------------

# Patterns for mrkdwn conversion (matched on raw markdown text)
_MRKDWN_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_MRKDWN_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MRKDWN_ITALIC = re.compile(r"(?<!\*)\*(\S(?:[^*]*\S)?)\*(?!\*)")
_MRKDWN_STRIKE = re.compile(r"~~(.+?)~~")
_MRKDWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def md_to_mrkdwn(text: str) -> str:
    """Best-effort Markdown → Slack mrkdwn conversion.

    Stashes code blocks and inline code first so their contents aren't
    touched, then applies formatting patterns. Falls back gracefully —
    partially converted text is better than a parse error.
    """
    # Stash code blocks (pass through unchanged)
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        idx = len(blocks)
        blocks.append(m.group(0))
        return f"\x00B{idx}\x00"

    text = _CODE_BLOCK.sub(_stash_block, text)

    # Stash inline code (pass through unchanged)
    inlines: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        idx = len(inlines)
        inlines.append(m.group(0))
        return f"\x00I{idx}\x00"

    text = _INLINE_CODE.sub(_stash_inline, text)

    # Links first (before bold/italic touch asterisks)
    text = _MRKDWN_LINK.sub(r"<\2|\1>", text)

    # Bold and headers produce *text* in mrkdwn — stash them so the
    # italic pass (which also looks for single *) doesn't clobber them.
    bolds: list[str] = []

    def _stash_bold(m: re.Match) -> str:
        idx = len(bolds)
        bolds.append(f"*{m.group(1)}*")
        return f"\x00D{idx}\x00"

    text = _MRKDWN_HEADER.sub(_stash_bold, text)
    text = _MRKDWN_BOLD.sub(_stash_bold, text)

    # Now italic is safe — no bold *…* left to confuse it
    text = _MRKDWN_ITALIC.sub(r"_\1_", text)
    text = _MRKDWN_STRIKE.sub(r"~\1~", text)

    # Collapse excessive blank lines (3+ newlines → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Restore stashed content (bolds, code blocks, inline code)
    for i, bold in enumerate(bolds):
        text = text.replace(f"\x00D{i}\x00", bold)
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", block)
    for i, inline in enumerate(inlines):
        text = text.replace(f"\x00I{i}\x00", inline)

    return text
