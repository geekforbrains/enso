"""Directory cache: matching, partial-event merges, and refresh-on-miss."""

from __future__ import annotations

import pytest
from conftest import FakeSlack

from enso import slack_cache
from enso.config import Paths

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
