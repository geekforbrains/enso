"""Pure string helpers over raw Slack message dicts, shared by the transport and CLI."""

from __future__ import annotations

import re
from collections.abc import Callable

# Message subtypes that carry no user content: channel lifecycle, message
# lifecycle, pins, reminders. Anything else falls through (plain messages,
# file_share, me_message, thread_broadcast) and the empty-text guard drops it.
IGNORED_SUBTYPES: frozenset[str] = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "message_replied",
        "channel_join",
        "channel_leave",
        "channel_archive",
        "channel_unarchive",
        "channel_name",
        "channel_purpose",
        "channel_topic",
        "channel_convert_to_private",
        "channel_convert_to_public",
        "channel_posting_permissions",
        "group_join",
        "group_leave",
        "group_archive",
        "group_unarchive",
        "group_name",
        "group_purpose",
        "group_topic",
        "pinned_item",
        "unpinned_item",
        "reminder_add",
        "ekm_access_denied",
        "file_mention",
        "file_comment",
        "document_mention",
    }
)

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)\|([^>]*)>")
# The ``<!command^arg|label>`` family: the broadcasts <!here>, <!channel> and
# <!everyone>, user groups as <!subteam^S…|@handle>, and rendered timestamps as
# <!date^…|Feb 18th>. Slack makes every one of them live on the way out.
_SPECIAL_RE = re.compile(r"<!(\w+)(?:\^([^|>]*))?(?:\|([^>]*))?>")
# Angle brackets would reintroduce live mention syntax through a display
# name; square brackets and line breaks could forge context labels.
_UNSAFE_NAME_RE = re.compile(r"[<>\[\]\r\n]+")


def safe_name(name: str | None) -> str:
    """Neutralize a user-controlled profile name for prompt interpolation."""
    if not name:
        return ""
    return " ".join(_UNSAFE_NAME_RE.sub(" ", name).split())


def unescape(text: str) -> str:
    """Undo Slack's ``&lt; &gt; &amp;`` escapes; ampersands last so ``&amp;lt;`` stays ``&lt;``."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def flatten_mentions(
    text: str,
    *,
    bot_user_id: str,
    lookup: Callable[[str], str],
    strip_addressing: bool = False,
) -> str:
    """Rewrite ``<@U…>`` as ``@name``, ``<#C…|name>`` as ``#name``, and ``<!…>`` as words.

    Raw mention syntax must never reach a prompt: outbound text is not escaped,
    so a token the model echoes back would ping the mentioned party — and for
    ``<!channel>`` that is everyone in the channel. Callers pass text that has
    been through :func:`unescape` exactly once, so a typed ``&lt;!channel&gt;``
    is revived before this pass rather than after it. With ``strip_addressing``
    a leading bot mention is treated as addressing and removed, which also keeps
    a following ``!command`` at position zero.
    """
    if strip_addressing and bot_user_id:
        leading = re.compile(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>[\s,:]*")
        while (stripped := leading.sub("", text, count=1)) != text:
            text = stripped

    def replace_user(match: re.Match[str]) -> str:
        user_id = match.group(1)
        name = safe_name(lookup(user_id))
        return f"@{name}" if name else f"@{user_id}"

    def replace_special(match: re.Match[str]) -> str:
        command, arg, label = match.group(1), match.group(2) or "", match.group(3) or ""
        display = safe_name(label)
        if command == "date":
            # Not a mention at all: keep the human-readable fallback Slack renders.
            return display
        return "@" + (display.lstrip("@") or arg or command)

    text = MENTION_RE.sub(replace_user, text)
    text = _CHANNEL_RE.sub(lambda m: f"#{safe_name(m.group(2)) or m.group(1)}", text)
    return _SPECIAL_RE.sub(replace_special, text)


def _is_shared_message(att: dict) -> bool:
    if att.get("is_msg_unfurl"):
        return True
    return bool(att.get("author_name") or att.get("author_id") or att.get("text"))


def _render_attachment(att: dict) -> str:
    author = att.get("author_name") or att.get("author_subname") or att.get("author_id") or ""
    channel = att.get("channel_name") or ""
    label = " ".join(p for p in (author, f"in #{channel}" if channel else "") if p)
    lines = [f"[Shared message — {label}]" if label else "[Shared message]"]
    body = (att.get("text") or att.get("fallback") or "").strip()
    if body:
        lines.append(body)
    if att.get("from_url"):
        lines.append(f"(link: {att['from_url']})")
    return "\n".join(lines)


def attachments_prompt(attachments: list[dict]) -> str:
    """Render forwarded messages, which Slack delivers in ``attachments`` not ``text``."""
    rendered = [
        _render_attachment(att)
        for att in attachments
        if isinstance(att, dict) and _is_shared_message(att)
    ]
    return "\n\n".join(r for r in rendered if r)


def attachment_files(attachments: list[dict]) -> list[dict]:
    """Files carried inside forwarded messages live under each attachment's ``files``."""
    return [
        file_info
        for att in attachments
        if isinstance(att, dict)
        for file_info in att.get("files") or []
        if isinstance(file_info, dict)
    ]


def message_text(msg: dict) -> str:
    """A history message's text plus any forwarded-message content, still escaped.

    Decoding is left to the caller so that it happens exactly once per path:
    unescaping here as well as at the :func:`flatten_mentions` boundary would
    turn a user who typed ``&lt;!channel&gt;`` into a live broadcast token.
    """
    text = msg.get("text", "")
    shared = attachments_prompt(msg.get("attachments") or [])
    return "\n".join(part for part in (text, shared) if part)
