"""Slack message text helpers shared by the transport and the CLI.

The transport module imports ``slack_bolt`` at module scope and raises when
the extra is missing, so anything the CLI also needs has to live outside it.
These helpers are pure string work over raw Slack message dicts: no network,
no configuration, no optional dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Slack message subtypes that aren't user-authored content — channel/group
# lifecycle, message lifecycle, pin/reminder noise, etc. Anything not in this
# set falls through, including the empty (plain message) case and content-
# bearing subtypes like file_share, me_message, and thread_broadcast. The
# downstream text/files guard drops anything genuinely empty.
#
# `document_mention` (canvas body @-mention) is intentionally ignored. Slack
# delivers it both as a message event and as an app_mention subtype (with a
# canvas file/section pointer rather than a chat-thread anchor), so both
# handlers consult this set. Threaded canvas comments arrive as regular
# app_mention events and still fall through.
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


def _is_shared_message(att: dict) -> bool:
    """True when an attachment carries a forwarded/shared message.

    Slack flags shares with ``is_msg_unfurl`` (the older ``is_share`` field is
    no longer in the schema), but we also accept any attachment carrying author
    or text content so we degrade gracefully if the flag is ever absent.
    """
    if att.get("is_msg_unfurl"):
        return True
    return bool(att.get("author_name") or att.get("author_id") or att.get("text"))


def _render_attachment(att: dict) -> str:
    """Render one shared-message attachment as prompt text."""
    author = att.get("author_name") or att.get("author_subname") or att.get("author_id") or ""
    channel = att.get("channel_name") or ""
    label_parts = [p for p in (author, f"in #{channel}" if channel else "") if p]
    label = " ".join(label_parts)
    header = f"[Shared message — {label}]" if label else "[Shared message]"

    lines = [header]
    body = (att.get("text") or att.get("fallback") or "").strip()
    if body:
        lines.append(body)
    link = att.get("from_url") or ""
    if link:
        lines.append(f"(link: {link})")
    return "\n".join(lines)


def _attachments_prompt(attachments: list[dict]) -> str:
    """Render forwarded/shared Slack messages into prompt text.

    When a user shares (forwards) a message, Slack delivers the original
    content in the event's ``attachments`` array — not in ``text``, which holds
    only the forwarder's own typed words. Each shared message arrives as an
    unfurl object carrying the author, source channel, body, and a permalink.
    """
    rendered = [
        _render_attachment(att)
        for att in attachments
        if isinstance(att, dict) and _is_shared_message(att)
    ]
    return "\n\n".join(r for r in rendered if r)


_MENTION_TOKEN_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

# Characters a profile name may not carry into a prompt: angle brackets would
# reintroduce live <@U…>/<!channel> syntax through a hostile display name,
# square brackets and line breaks could forge the [user …]: context labels.
_UNSAFE_NAME_RE = re.compile(r"[<>\[\]\r\n]+")


def _safe_name(name: str | None) -> str:
    """Neutralize a user-controlled profile name for prompt interpolation."""
    if not name:
        return ""
    return " ".join(_UNSAFE_NAME_RE.sub(" ", name).split())


def _flatten_mention_text(
    text: str,
    *,
    bot_user_id: str,
    bot_label: str,
    lookup: Callable[[str], str],
    strip_addressing: bool = True,
) -> str:
    """Rewrite ``<@U…>`` mention tokens as inert readable text.

    Raw mention syntax must never reach a prompt: outbound mrkdwn is not
    escaped, so a token the model echoes back would ping the mentioned
    person. With ``strip_addressing`` a leading bot mention is treated as
    addressing rather than content and removed, which also keeps a
    following ``!command`` at position zero. Remaining bot mentions become
    ``@<bot name>``; anyone else becomes ``@<name> (<ID>)``, or ``@<ID>``
    when the directory cache has no name. ``<!here>``-style specials carry
    no user identity and pass through.
    """
    if strip_addressing and bot_user_id:
        leading = re.compile(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>[\s,:]*")
        while True:
            stripped = leading.sub("", text, count=1)
            if stripped == text:
                break
            text = stripped

    def _replace(match: re.Match) -> str:
        user_id = match.group(1)
        if bot_user_id and user_id == bot_user_id:
            return f"@{_safe_name(bot_label)}" if bot_label else f"@{user_id}"
        name = _safe_name(lookup(user_id))
        return f"@{name} ({user_id})" if name else f"@{user_id}"

    return _MENTION_TOKEN_RE.sub(_replace, text)


def _message_context_text(msg: dict) -> str:
    """Combine a history message's text with any forwarded-message content.

    Forwarded messages in fetched channel/thread history carry their content in
    ``attachments`` just like live events, so context rendering must surface it
    too — otherwise the agent sees a blank line where a shared message was.
    """
    text = msg.get("text", "")
    shared = _attachments_prompt(msg.get("attachments") or [])
    return "\n".join(part for part in (text, shared) if part)
