"""Convert standard Markdown to Telegram-safe HTML."""

from __future__ import annotations

import re
from html import escape

# Fenced code blocks: ```lang\n...\n```
_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
# Inline code: `...`
_INLINE_CODE = re.compile(r"`([^`]+)`")
# Bold: **...**
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Italic: *...* (but not inside bold)
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
# Strikethrough: ~~...~~
_STRIKE = re.compile(r"~~(.+?)~~")
# Links: [text](url)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# Headers: # ... (convert to bold)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def md_to_html(text: str) -> str:
    """Best-effort Markdown → Telegram HTML conversion.

    Handles code blocks, inline code, bold, italic, strikethrough,
    links, and headers. Escapes HTML entities in non-code text.
    Falls back gracefully — partially converted text is better than
    a parse error.
    """
    # Extract code blocks first so their contents aren't processed
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        lang = m.group(1)
        code = escape(m.group(2).strip())
        placeholder = f"\x00CODEBLOCK{len(blocks)}\x00"
        if lang:
            blocks.append(f'<pre><code class="language-{escape(lang)}">{code}</code></pre>')
        else:
            blocks.append(f"<pre>{code}</pre>")
        return placeholder

    text = _CODE_BLOCK.sub(_stash_block, text)

    # Extract inline code
    inlines: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        placeholder = f"\x00INLINE{len(inlines)}\x00"
        inlines.append(f"<code>{escape(m.group(1))}</code>")
        return placeholder

    text = _INLINE_CODE.sub(_stash_inline, text)

    # Escape remaining HTML entities
    text = escape(text)

    # Apply formatting (order matters)
    text = _HEADER.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)

    # Restore code blocks and inline code
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)
    for i, inline in enumerate(inlines):
        text = text.replace(f"\x00INLINE{i}\x00", inline)

    return text
