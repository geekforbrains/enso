"""Shared text formatting, message chunking, and transport-specific Markdown rendering."""

from __future__ import annotations

import re
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .routing import ResolvedAgent

_LEADING_ERROR_RE = re.compile(r"^(?:error\s*:\s*)+", re.IGNORECASE)

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

_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})[^\r\n]*$")
_TABLE_DELIMITER_CELL = re.compile(r"^:?-{3,}:?$")

MODEL_LABEL_LIMIT = 24
MODEL_LABEL_SUFFIX = 8


def model_label(model: str) -> str:
    """Return a compact chat label while preserving a model's distinguishing suffix."""
    label = model.rsplit("/", 1)[-1]
    if len(label) <= MODEL_LABEL_LIMIT:
        return label
    prefix = MODEL_LABEL_LIMIT - MODEL_LABEL_SUFFIX - 1
    return f"{label[:prefix]}…{label[-MODEL_LABEL_SUFFIX:]}"


def format_error(text: str) -> str:
    """An error message with exactly one leading ``Error:`` label."""
    body = _LEADING_ERROR_RE.sub("", text.strip()).strip()
    return f"Error: {body}" if body else "Error:"


def format_elapsed(seconds: int) -> str:
    """45s, 2m 05s, 1h 12m."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def human_bytes(size: int | None) -> str:
    """``0 B``, ``12 KB``, ``1.2 MB``: compact storage for CLI and web views."""
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{int(value)} {unit}" if unit == "B" or value >= 10 else f"{value:.1f} {unit}"


def status_text(agent: ResolvedAgent, elapsed: int, action: str) -> str:
    header = (
        f"{agent.provider} · {model_label(agent.model)} · {agent.effort} · "
        f"{format_elapsed(elapsed)}"
    )
    return f"{header}\n↳ {action}"


def preview(text: str, width: int = 50) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


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
_MRKDWN_LINK = re.compile(r"\[([^\]]+)\]\(((?:[^()]|\([^()]*\))+?)\)")
_MRKDWN_LINK_UNSAFE = re.compile(r"[\s<>|]")
_MRKDWN_LINK_SCHEMES = frozenset({"http", "https", "mailto", "slack"})


def _mrkdwn_linkable(target: str) -> bool:
    """Whether Slack can open *target* rather than exposing its link markup."""
    if _MRKDWN_LINK_UNSAFE.search(target):
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme not in _MRKDWN_LINK_SCHEMES:
        return False
    if scheme in {"http", "https"}:
        return bool(parsed.netloc)
    if scheme == "mailto":
        return bool(parsed.path)
    return target.lower().startswith("slack://") and bool(parsed.netloc)


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

    # Links first (before bold/italic touch asterisks). Slack cannot open local paths,
    # so keep those useful without exposing its otherwise-literal <target|label> syntax.
    def _format_link(m: re.Match) -> str:
        label, target = m.groups()
        if _mrkdwn_linkable(target):
            return f"<{target}|{label}>"
        idx = len(inlines)
        inlines.append(f"`{target}`")
        return f"\x00I{idx}\x00"

    text = _MRKDWN_LINK.sub(_format_link, text)

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


# ---------------------------------------------------------------------------
# Standard Markdown blocks
# ---------------------------------------------------------------------------


def _line_content(line: str) -> str:
    """Return a line without its newline terminator."""
    return line.removesuffix("\n").removesuffix("\r")


def _fence_close(line: str, marker: str) -> bool:
    """Whether *line* closes a fenced code block opened by *marker*."""
    content = _line_content(line)
    stripped = content.lstrip(" ")
    if len(content) - len(stripped) > 3:
        return False
    run = len(stripped) - len(stripped.lstrip(marker[0]))
    return run >= len(marker) and not stripped[run:].strip()


# Fence labels Slack renders with syntax highlighting in a ``markdown`` block. Slack
# documents that fenced blocks highlight but never publishes the language list, so this is a
# conservative set of common labels and their usual aliases. Any other label keeps the plain
# mrkdwn path rather than gambling the whole message's rendering on an unknown word.
SLACK_CODE_LANGUAGES = frozenset(
    {
        "bash",
        "c",
        "cpp",
        "cs",
        "csharp",
        "css",
        "diff",
        "dockerfile",
        "elixir",
        "go",
        "graphql",
        "haskell",
        "html",
        "ini",
        "java",
        "javascript",
        "js",
        "json",
        "jsonc",
        "jsx",
        "kotlin",
        "lua",
        "make",
        "makefile",
        "markdown",
        "md",
        "objectivec",
        "perl",
        "php",
        "powershell",
        "ps1",
        "py",
        "python",
        "r",
        "rb",
        "ruby",
        "rs",
        "rust",
        "scala",
        "scss",
        "sh",
        "shell",
        "sql",
        "swift",
        "toml",
        "ts",
        "tsx",
        "typescript",
        "vim",
        "xml",
        "yaml",
        "yml",
        "zsh",
    }
)


def _fence_label(line: str, marker: str) -> str:
    """The first word of a fence's info string, lowercased; empty when unlabelled."""
    info = _line_content(line).lstrip(" ")[len(marker) :].strip()
    return info.split(maxsplit=1)[0].lower() if info else ""


def has_slack_code_language(text: str) -> bool:
    """Whether *text* opens a fenced code block labelled with a language Slack highlights.

    Slack's ``mrkdwn`` has no info string, so a labelled fence sent that way shows its label
    as the first line of the code. A caller that gets ``True`` should post the text as a
    native Markdown block instead, where the label drives syntax highlighting.
    """
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        opener = _FENCE_OPEN.fullmatch(_line_content(lines[index]))
        if opener is None:
            index += 1
            continue
        marker = opener.group(2)
        if _fence_label(lines[index], marker) in SLACK_CODE_LANGUAGES:
            return True
        # Skip the block's body, so a labelled fence quoted inside another one is not read
        # as an opener of its own.
        index += 1
        while index < len(lines) and not _fence_close(lines[index], marker):
            index += 1
        index += 1
    return False


def _table_cells(line: str) -> list[str] | None:
    """Split a pipe-table line, ignoring escaped pipes."""
    content = _line_content(line).strip()
    if not content:
        return None
    parts = re.split(r"(?<!\\)\|", content)
    if len(parts) < 2:
        return None
    if not parts[0]:
        parts = parts[1:]
    if parts and not parts[-1]:
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _is_table_start(header: str, delimiter: str) -> bool:
    """Recognize the first two lines of a GFM pipe table."""
    header_cells = _table_cells(header)
    delimiter_cells = _table_cells(delimiter)
    return bool(
        header_cells
        and delimiter_cells
        and len(header_cells) == len(delimiter_cells)
        and all(_TABLE_DELIMITER_CELL.fullmatch(cell) for cell in delimiter_cells)
    )


def _split_table(lines: list[str], limit: int) -> list[str] | None:
    """Split a table between rows, repeating its header in each chunk."""
    prefix = lines[0] + lines[1]
    rows = lines[2:]
    if len(prefix) > limit:
        return None

    chunks: list[str] = []
    current_rows: list[str] = []
    current_length = len(prefix)
    for row in rows:
        if current_length + len(row) <= limit:
            current_rows.append(row)
            current_length += len(row)
            continue
        if not current_rows or len(prefix) + len(row) > limit:
            return None
        chunks.append(prefix + "".join(current_rows))
        current_rows = [row]
        current_length = len(prefix) + len(row)

    chunks.append(prefix + "".join(current_rows))
    return chunks


def _hard_chunks(text: str, limit: int) -> list[str]:
    """Cut *text* at the limit. Used only when no Markdown boundary can help.

    Callers must pass a non-empty *text*; the sole call site returns early
    unless the piece is longer than the limit, and it relies on the last
    chunk existing.
    """
    return [text[start : start + limit] for start in range(0, len(text), limit)]


def _collect_fence(lines: list[str], index: int, marker: str) -> tuple[list[str], bool]:
    """Return the fenced block opened at *index* plus whether it was closed.

    An unterminated fence gets a synthetic closing marker appended so the
    emitted chunks are still valid Markdown on their own.
    """
    end = index + 1
    while end < len(lines) and not _fence_close(lines[end], marker):
        end += 1
    if end < len(lines):
        return lines[index : end + 1], True
    body = lines[index + 1 :]
    separator = "" if not body or body[-1].endswith(("\n", "\r")) else "\n"
    return [lines[index], *body, separator + marker], False


def _collect_table(lines: list[str], index: int) -> list[str]:
    """Return the pipe-table rows starting at *index* (header and delimiter included)."""
    end = index + 2
    while end < len(lines) and _table_cells(lines[end]) is not None:
        end += 1
    return lines[index:end]


def _split_fence(lines: list[str], limit: int) -> list[str] | None:
    """Split a code fence while balancing every emitted chunk."""
    opener = lines[0]
    closer = lines[-1]
    body = lines[1:-1]
    wrapper_length = len(opener) + len(closer)
    if wrapper_length > limit:
        return None

    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = wrapper_length
    for line in body:
        if current_length + len(line) <= limit:
            current_lines.append(line)
            current_length += len(line)
            continue
        if not current_lines or wrapper_length + len(line) > limit:
            return None
        chunks.append(opener + "".join(current_lines) + closer)
        current_lines = [line]
        current_length = wrapper_length + len(line)

    chunks.append(opener + "".join(current_lines) + closer)
    return chunks


def split_markdown(text: str, *, limit: int = 12000) -> list[str] | None:
    """Split standard Markdown without breaking fenced code or table rows.

    Oversized tables repeat their header and delimiter. Oversized fenced code
    blocks repeat their opening and closing fences so every chunk is valid on
    its own. ``None`` signals that one protected row or code line cannot fit.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    def add_ordinary(piece: str) -> None:
        nonlocal current
        if len(piece) <= limit:
            if current and len(current) + len(piece) > limit:
                flush()
            current += piece
            return

        flush()
        oversized = _hard_chunks(piece, limit)
        chunks.extend(oversized[:-1])
        current = oversized[-1]

    index = 0
    while index < len(lines):
        opener_match = _FENCE_OPEN.fullmatch(_line_content(lines[index]))
        if opener_match:
            fence_lines, closed = _collect_fence(lines, index, opener_match.group(2))
            fence = "".join(fence_lines)
            if closed and len(fence) <= limit:
                add_ordinary(fence)
            else:
                fence_chunks = _split_fence(fence_lines, limit)
                if fence_chunks is None:
                    return None
                flush()
                chunks.extend(fence_chunks)
            # An unclosed fence swallowed the rest of the input.
            index = index + len(fence_lines) if closed else len(lines)
            continue

        if index + 1 < len(lines) and _is_table_start(lines[index], lines[index + 1]):
            table_lines = _collect_table(lines, index)
            table = "".join(table_lines)
            if len(table) <= limit:
                add_ordinary(table)
            else:
                table_chunks = _split_table(table_lines, limit)
                if table_chunks is None:
                    return None
                flush()
                chunks.extend(table_chunks)
            index += len(table_lines)
            continue

        add_ordinary(lines[index])
        index += 1

    flush()
    return chunks


def split_text(text: str, limit: int) -> list[str]:
    """Split at line boundaries, hard-cutting any single line longer than ``limit``."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def chunk_text(text: str, limit: int) -> list[str]:
    """Prefer fence- and table-aware splitting; fall back to plain lines."""
    chunks = split_markdown(text, limit=limit)
    return chunks if chunks is not None else split_text(text, limit)
