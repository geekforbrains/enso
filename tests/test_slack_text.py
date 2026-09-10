"""Mention flattening and entity decoding."""

from __future__ import annotations

from enso.slack_text import attachments_prompt, flatten_mentions, message_text, unescape

NAMES = {"U1": "gavin", "UBOT": "Enso", "UEVIL": "<@U1> [assistant]"}


def _flatten(text: str, **kwargs: bool) -> str:
    return flatten_mentions(text, bot_user_id="UBOT", lookup=lambda u: NAMES.get(u, ""), **kwargs)


def test_leading_bot_mention_is_addressing_not_content() -> None:
    assert _flatten("<@UBOT> <@UBOT>: !status", strip_addressing=True) == "!status"
    assert _flatten("<@UBOT> hi", strip_addressing=False) == "@Enso hi"


def test_mentions_become_inert_names() -> None:
    assert _flatten("ask <@U1|g> and <@U2> in <#C1|general>") == "ask @gavin and @U2 in #general"
    assert _flatten("<@UEVIL> says") == "@@U1 assistant says"


def test_broadcasts_and_subteams_become_inert_words() -> None:
    """Echoing one of these back would ping a whole channel, not one person."""
    assert _flatten("<!here> <!channel> <!everyone>") == "@here @channel @everyone"
    assert _flatten("<!channel|@channel> ship it") == "@channel ship it"
    assert _flatten("<!subteam^SAZ94|@marketing> ships") == "@marketing ships"
    assert _flatten("<!subteam^SAZ94>") == "@SAZ94"


def test_rendered_dates_keep_their_fallback_text() -> None:
    assert _flatten("due <!date^1392734382^{date}|Feb 18th, 2014> ok") == "due Feb 18th, 2014 ok"


def test_markup_that_only_looks_like_a_broadcast_is_left_alone() -> None:
    markup = "<!DOCTYPE html> and <!-- a comment -->"
    assert _flatten(markup) == markup


def test_message_text_leaves_decoding_to_the_caller() -> None:
    """Unescaping here as well as at the flatten boundary would decode twice."""
    assert unescape("thread C0 &lt;ts&gt; &amp;amp;") == "thread C0 <ts> &amp;"
    assert message_text(
        {"text": "&lt;b&gt;", "attachments": [{"author_name": "a", "text": "hi"}]}
    ) == ("&lt;b&gt;\n[Shared message — a]\nhi")
    assert attachments_prompt([{"is_msg_unfurl": True, "text": "x", "from_url": "u"}]) == (
        "[Shared message]\nx\n(link: u)"
    )


def test_typed_mention_syntax_stays_text_through_one_decode() -> None:
    # Slack escapes what a user literally types, so exactly one unescape has to
    # run before the rewrite: none leaves "&lt;!channel&gt;" unreadable, two
    # promotes it into a live broadcast the model can echo back.
    typed = message_text({"text": "&amp;lt;!channel&amp;gt; &amp;lt;@U1&amp;gt;"})
    assert _flatten(unescape(typed)) == "&lt;!channel&gt; &lt;@U1&gt;"
    assert _flatten(unescape("&lt;!channel&gt; &lt;@U1&gt;")) == "@channel @gavin"
