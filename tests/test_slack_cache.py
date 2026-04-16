"""Tests for the Slack directory cache module."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import pytest

from enso import slack_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_user(
    *,
    uid: str = "U1",
    name: str = "gavin",
    real: str = "Gavin Vickery",
    display: str = "Gavin",
    email: str = "gavin@example.com",
    is_bot: bool = False,
    deleted: bool = False,
) -> dict:
    return {
        "id": uid,
        "name": name,
        "real_name": real,
        "deleted": deleted,
        "is_bot": is_bot,
        "profile": {
            "real_name": real,
            "display_name": display,
            "email": email,
        },
    }


def _raw_channel(
    *,
    cid: str = "C1",
    name: str = "general",
    is_private: bool = False,
    archived: bool = False,
    is_member: bool = True,
    members: int = 5,
    topic: str = "",
) -> dict:
    return {
        "id": cid,
        "name": name,
        "is_private": is_private,
        "is_archived": archived,
        "is_member": is_member,
        "num_members": members,
        "topic": {"value": topic},
        "purpose": {"value": ""},
    }


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


class TestLoadSave:
    def test_load_missing_returns_empty(self, tmp_enso):
        data = slack_cache.load()
        assert data["team_id"] == ""
        assert data["users"]["items"] == {}
        assert data["channels"]["items"] == {}
        assert data["dm_cache"] == {}

    def test_save_and_reload_round_trip(self, tmp_enso):
        cache = slack_cache._empty_cache()
        cache["users"]["items"]["U1"] = slack_cache._normalise_user(_raw_user())
        slack_cache.save(cache)
        data = slack_cache.load()
        assert data["users"]["items"]["U1"]["name"] == "gavin"

    def test_load_fills_missing_top_level_keys(self, tmp_enso):
        os.makedirs(os.path.dirname(slack_cache.CACHE_FILE), exist_ok=True)
        with open(slack_cache.CACHE_FILE, "w") as f:
            json.dump({"users": {"items": {"U1": {"id": "U1"}}}}, f)
        data = slack_cache.load()
        assert "channels" in data
        assert "dm_cache" in data

    def test_load_corrupt_file_returns_empty(self, tmp_enso):
        os.makedirs(os.path.dirname(slack_cache.CACHE_FILE), exist_ok=True)
        with open(slack_cache.CACHE_FILE, "w") as f:
            f.write("{ not json")
        data = slack_cache.load()
        assert data["users"]["items"] == {}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_user_pulls_from_profile(self):
        out = slack_cache._normalise_user(_raw_user(email="gavin@x.com"))
        assert out["email"] == "gavin@x.com"
        assert out["display_name"] == "Gavin"

    def test_user_missing_profile_fields(self):
        raw = {"id": "U1", "name": "bob", "profile": {}}
        out = slack_cache._normalise_user(raw)
        assert out["email"] == ""
        assert out["display_name"] == ""
        assert out["real_name"] == ""
        assert out["is_bot"] is False
        assert out["deleted"] is False

    def test_channel_truncates_long_topic(self):
        raw = _raw_channel(topic="x" * 500)
        out = slack_cache._normalise_channel(raw)
        assert len(out["topic"]) == 200

    def test_channel_handles_missing_topic_purpose(self):
        raw = {"id": "C1", "name": "g"}
        out = slack_cache._normalise_channel(raw)
        assert out["topic"] == ""
        assert out["purpose"] == ""


# ---------------------------------------------------------------------------
# Matching / lookups (no network)
# ---------------------------------------------------------------------------


def _seed_cache(tmp_enso):
    cache = slack_cache._empty_cache()
    cache["users"]["fetched_at"] = time.time()
    cache["users"]["items"] = {
        "U1": slack_cache._normalise_user(_raw_user(uid="U1", name="gavin")),
        "U2": slack_cache._normalise_user(_raw_user(
            uid="U2", name="sarah", real="Sarah Chen",
            display="sarahc", email="sarah@example.com",
        )),
        "U3": slack_cache._normalise_user(_raw_user(
            uid="U3", name="hermy", real="Hermy Bot",
            display="", email="", is_bot=True,
        )),
    }
    cache["channels"]["fetched_at"] = time.time()
    cache["channels"]["items"] = {
        "C1": slack_cache._normalise_channel(_raw_channel(cid="C1", name="general")),
        "C2": slack_cache._normalise_channel(_raw_channel(
            cid="C2", name="daily-standup", is_member=False,
        )),
    }
    slack_cache.save(cache)


class TestLookup:
    def test_lookup_user_by_name(self, tmp_enso):
        _seed_cache(tmp_enso)
        matches = slack_cache.lookup_user("gavin")
        assert len(matches) == 1
        assert matches[0]["id"] == "U1"

    def test_lookup_user_by_email(self, tmp_enso):
        _seed_cache(tmp_enso)
        matches = slack_cache.lookup_user("sarah@example.com")
        assert [m["id"] for m in matches] == ["U2"]

    def test_lookup_user_by_real_name_partial(self, tmp_enso):
        _seed_cache(tmp_enso)
        matches = slack_cache.lookup_user("Chen")
        assert [m["id"] for m in matches] == ["U2"]

    def test_lookup_user_case_insensitive(self, tmp_enso):
        _seed_cache(tmp_enso)
        assert slack_cache.lookup_user("GAVIN")[0]["id"] == "U1"

    def test_lookup_user_by_id(self, tmp_enso):
        _seed_cache(tmp_enso)
        assert slack_cache.lookup_user("U1")[0]["id"] == "U1"

    def test_lookup_user_miss_without_token_stays_empty(self, tmp_enso):
        _seed_cache(tmp_enso)
        assert slack_cache.lookup_user("nobody") == []

    def test_lookup_channel_strip_hash(self, tmp_enso):
        _seed_cache(tmp_enso)
        matches = slack_cache.lookup_channel("#general")
        assert [m["id"] for m in matches] == ["C1"]

    def test_lookup_channel_partial(self, tmp_enso):
        _seed_cache(tmp_enso)
        matches = slack_cache.lookup_channel("stand")
        assert [m["id"] for m in matches] == ["C2"]


# ---------------------------------------------------------------------------
# Event-driven mutations
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_user_change_inserts(self, tmp_enso):
        slack_cache.apply_user_change(_raw_user(uid="U99", name="new"))
        data = slack_cache.load()
        assert data["users"]["items"]["U99"]["name"] == "new"

    def test_apply_user_change_updates(self, tmp_enso):
        _seed_cache(tmp_enso)
        slack_cache.apply_user_change(_raw_user(
            uid="U1", name="gavin", real="Gavin Renamed",
        ))
        data = slack_cache.load()
        assert data["users"]["items"]["U1"]["real_name"] == "Gavin Renamed"

    def test_apply_user_change_ignores_empty_id(self, tmp_enso):
        slack_cache.apply_user_change({"name": "ghost"})
        data = slack_cache.load()
        assert data["users"]["items"] == {}

    def test_apply_channel_upsert_merges_existing(self, tmp_enso):
        _seed_cache(tmp_enso)
        # Partial update (just the archive flag) should preserve name/etc.
        slack_cache.apply_channel_upsert(
            {"id": "C1", "is_archived": True, "name": "general"},
        )
        data = slack_cache.load()
        entry = data["channels"]["items"]["C1"]
        assert entry["is_archived"] is True
        assert entry["name"] == "general"

    def test_apply_channel_delete_removes(self, tmp_enso):
        _seed_cache(tmp_enso)
        slack_cache.apply_channel_delete("C1")
        data = slack_cache.load()
        assert "C1" not in data["channels"]["items"]

    def test_set_channel_is_member_flips_flag(self, tmp_enso):
        _seed_cache(tmp_enso)
        slack_cache.set_channel_is_member("C2", True)
        data = slack_cache.load()
        assert data["channels"]["items"]["C2"]["is_member"] is True

    def test_set_channel_is_member_ignores_unknown(self, tmp_enso):
        # Should not create a bare entry for a channel we don't know about.
        slack_cache.set_channel_is_member("C_MYSTERY", True)
        data = slack_cache.load()
        assert "C_MYSTERY" not in data["channels"]["items"]


# ---------------------------------------------------------------------------
# Refresh (API mocked)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRefresh:
    def test_refresh_users_overwrites_entries(self, tmp_enso):
        _seed_cache(tmp_enso)  # cache has U1, U2, U3
        payload = {
            "ok": True,
            "members": [_raw_user(uid="U1", name="gavin")],
            "response_metadata": {"next_cursor": ""},
        }
        with patch("enso.slack_cache.urllib.request.urlopen", return_value=FakeResponse(payload)):
            slack_cache.refresh_users("xoxb-fake")
        data = slack_cache.load()
        assert set(data["users"]["items"]) == {"U1"}  # U2, U3 gone

    def test_refresh_channels_uses_exclude_archived(self, tmp_enso):
        calls: list[str] = []

        def fake_urlopen(req, *args, **kwargs):
            calls.append(req.full_url)
            return FakeResponse({
                "ok": True, "channels": [_raw_channel(cid="C9", name="new")],
                "response_metadata": {"next_cursor": ""},
            })

        with patch("enso.slack_cache.urllib.request.urlopen", side_effect=fake_urlopen):
            slack_cache.refresh_channels("xoxb-fake")
        assert any("exclude_archived=true" in url for url in calls)
        data = slack_cache.load()
        assert "C9" in data["channels"]["items"]

    def test_lookup_user_refreshes_on_miss(self, tmp_enso):
        # Seed with a stale fetched_at (well past the 60s guard) and no match
        cache = slack_cache._empty_cache()
        cache["users"]["fetched_at"] = time.time() - 1000
        slack_cache.save(cache)

        payload = {
            "ok": True,
            "members": [_raw_user(uid="U1", name="gavin")],
            "response_metadata": {"next_cursor": ""},
        }
        with patch("enso.slack_cache.urllib.request.urlopen", return_value=FakeResponse(payload)):
            matches = slack_cache.lookup_user("gavin", token="xoxb-fake")
        assert len(matches) == 1

    def test_lookup_user_no_double_refresh_within_guard(self, tmp_enso):
        # Just refreshed — a miss should NOT trigger another API call.
        cache = slack_cache._empty_cache()
        cache["users"]["fetched_at"] = time.time()
        slack_cache.save(cache)

        with patch("enso.slack_cache.urllib.request.urlopen") as mock_open:
            matches = slack_cache.lookup_user("gavin", token="xoxb-fake")
        assert matches == []
        mock_open.assert_not_called()

    def test_paginate_follows_cursor(self, tmp_enso):
        pages = [
            {"ok": True, "members": [_raw_user(uid="U1", name="a")],
             "response_metadata": {"next_cursor": "page2"}},
            {"ok": True, "members": [_raw_user(uid="U2", name="b")],
             "response_metadata": {"next_cursor": ""}},
        ]
        call_urls: list[str] = []

        def fake_urlopen(req, *args, **kwargs):
            call_urls.append(req.full_url)
            return FakeResponse(pages[len(call_urls) - 1])

        with patch("enso.slack_cache.urllib.request.urlopen", side_effect=fake_urlopen):
            slack_cache.refresh_users("xoxb-fake")
        assert len(call_urls) == 2
        assert "cursor=page2" in call_urls[1]
        data = slack_cache.load()
        assert set(data["users"]["items"]) == {"U1", "U2"}


# ---------------------------------------------------------------------------
# open_dm
# ---------------------------------------------------------------------------


class TestOpenDm:
    def test_cached_dm_returned_without_api_call(self, tmp_enso):
        cache = slack_cache._empty_cache()
        cache["dm_cache"]["U1"] = "D1"
        slack_cache.save(cache)

        with patch("enso.slack_cache.urllib.request.urlopen") as mock_open:
            assert slack_cache.open_dm("U1", "xoxb-fake") == "D1"
        mock_open.assert_not_called()

    def test_opens_and_caches(self, tmp_enso):
        payload = {"ok": True, "channel": {"id": "D999"}}
        with patch("enso.slack_cache.urllib.request.urlopen", return_value=FakeResponse(payload)):
            result = slack_cache.open_dm("U42", "xoxb-fake")
        assert result == "D999"
        data = slack_cache.load()
        assert data["dm_cache"]["U42"] == "D999"

    def test_raises_on_api_error(self, tmp_enso):
        payload = {"ok": False, "error": "user_not_found"}
        with patch("enso.slack_cache.urllib.request.urlopen", return_value=FakeResponse(payload)):
            with pytest.raises(RuntimeError, match="user_not_found"):
                slack_cache.open_dm("U42", "xoxb-fake")


# ---------------------------------------------------------------------------
# whois
# ---------------------------------------------------------------------------


class TestWhois:
    def test_hits_cache_first(self, tmp_enso):
        _seed_cache(tmp_enso)
        with patch("enso.slack_cache.urllib.request.urlopen") as mock_open:
            entry = slack_cache.whois("U1", token="xoxb-fake")
        assert entry["name"] == "gavin"
        mock_open.assert_not_called()

    def test_fetches_on_miss_and_caches(self, tmp_enso):
        payload = {"ok": True, "user": _raw_user(uid="U50", name="new")}
        with patch("enso.slack_cache.urllib.request.urlopen", return_value=FakeResponse(payload)):
            entry = slack_cache.whois("U50", token="xoxb-fake")
        assert entry["name"] == "new"
        data = slack_cache.load()
        assert "U50" in data["users"]["items"]

    def test_returns_none_on_api_failure(self, tmp_enso):
        payload = {"ok": False, "error": "user_not_found"}
        with patch("enso.slack_cache.urllib.request.urlopen", return_value=FakeResponse(payload)):
            assert slack_cache.whois("U_NOPE", token="xoxb-fake") is None
