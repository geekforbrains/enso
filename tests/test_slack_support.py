"""Slack support modules: the directory cache, and mention/entity handling."""

from __future__ import annotations

import pytest
from conftest import FakeSlack

from enso import slack_cache
from enso.config import Paths
from enso.slack_text import attachments_prompt, flatten_mentions, message_text, unescape

# -- The on-disk directory cache --

USERS = [
    {"id": "U1", "name": "gavin", "real_name": "Gavin V", "profile": {"email": "g@x.io"}},
    {"id": "UB", "name": "enso", "is_bot": True, "profile": {"display_name": "Enso"}},
]
CHANNELS = [
    {"id": "C1", "name": "general", "is_member": True, "topic": {"value": "Talk"}},
    {"id": "G1", "name": "ops", "is_private": True},
]


def _pages(key: str, pages: list[list[dict]]):
    """Answer a paginated list call one page at a time."""
    calls = iter(pages)

    def respond(kwargs: dict) -> dict:
        page = next(calls)
        more = kwargs.get("cursor") is None and len(pages) > 1
        return {key: page, "response_metadata": {"next_cursor": "next" if more else ""}}

    return respond


@pytest.mark.parametrize(
    ("query", "ids"),
    [("gav", ["U1"]), ("G@X.IO", ["U1"]), ("enso", ["UB"]), ("U", ["U1", "UB"]), ("zzz", [])],
)
def test_find_users(query: str, ids: list[str]) -> None:
    data = {**slack_cache._empty()}
    data["users"]["items"] = {u["id"]: slack_cache.user_entry(u) for u in USERS}
    assert [u["id"] for u in slack_cache.find_users(data, query)] == ids


async def test_lookup_refreshes_on_miss_at_most_once_a_minute(enso_home: Paths) -> None:
    client = FakeSlack(
        users_list=_pages("members", [USERS[:1], USERS[1:]]),
        conversations_list=_pages("channels", [CHANNELS]),
    )
    found = await slack_cache.lookup_users(enso_home, client, "enso")
    assert [u["id"] for u in found] == ["UB"] and len(client.sent("users_list")) == 2
    assert await slack_cache.lookup_users(enso_home, client, "nobody") == []
    assert len(client.sent("users_list")) == 2  # just refreshed; no second round trip
    channels = await slack_cache.lookup_channels(enso_home, client, "#OPS")
    assert [c["id"] for c in channels] == ["G1"] and channels[0]["is_private"]
    data = slack_cache.load(enso_home)
    assert data["channels"]["items"]["C1"]["topic"] == "Talk"


async def test_whois_and_open_dm_fill_the_cache(enso_home: Paths) -> None:
    client = FakeSlack(users_info={"user": USERS[0]}, conversations_open={"channel": {"id": "D1"}})
    assert await slack_cache.names(enso_home, client, ["U1", "U1"]) == {"U1": "Gavin V"}
    assert await slack_cache.whois(enso_home, client, "U1") == slack_cache.user_entry(USERS[0])
    assert len(client.sent("users_info")) == 1
    assert await slack_cache.open_dm(enso_home, client, "U1") == "D1"
    assert await slack_cache.open_dm(enso_home, client, "U1") == "D1"
    assert len(client.sent("conversations_open")) == 1
    client.responses["users_info"] = RuntimeError("user_not_found")
    assert await slack_cache.whois(enso_home, client, "U9") is None


def test_channel_events_merge_partially_and_delete(enso_home: Paths) -> None:
    slack_cache.put_channel(enso_home, CHANNELS[0])
    slack_cache.put_channel(enso_home, {"id": "C1", "name": "renamed"})  # channel_rename
    slack_cache.put_channel(enso_home, {"id": "C1", "is_archived": True})  # channel_archive
    slack_cache.put_channel(enso_home, {"id": "C9", "is_archived": True})  # unknown: ignored
    items = slack_cache.load(enso_home)["channels"]["items"]
    assert (
        items["C1"]["name"] == "renamed" and items["C1"]["is_member"] and items["C1"]["is_archived"]
    )
    assert "C9" not in items
    slack_cache.drop_channel(enso_home, "C1")
    assert slack_cache.load(enso_home)["channels"]["items"] == {}


# -- Mention flattening and entity decoding --

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
