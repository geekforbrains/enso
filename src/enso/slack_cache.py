"""Slack directory cache (``$ENSO_HOME/cache/slack.json``): users, channels, opened DMs.

The transport and the ``enso slack`` CLI share the file, so every change is a
load-modify-save with an atomic replace; last write wins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from . import slack_text
from .config import Paths

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient

log = logging.getLogger(__name__)

MIN_REFRESH_SECONDS = 60  # a lookup that misses refreshes its list at most this often
PAGE_SIZE = 200
USER_SEARCH_FIELDS = ("id", "name", "real_name", "display_name", "email")


def _empty() -> dict[str, Any]:
    return {
        "users": {"fetched_at": 0.0, "items": {}},
        "channels": {"fetched_at": 0.0, "items": {}},
        "dms": {},
    }


def load(paths: Paths) -> dict[str, Any]:
    try:
        with open(paths.slack_cache, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        log.warning("unreadable slack cache %s (%s); starting empty", paths.slack_cache, exc)
        return _empty()
    return {**_empty(), **data} if isinstance(data, dict) else _empty()


def save(paths: Paths, data: dict[str, Any]) -> None:
    paths.cache.mkdir(parents=True, exist_ok=True)
    tmp = paths.slack_cache.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, paths.slack_cache)


# -- Entries --


def user_entry(raw: dict) -> dict:
    profile = raw.get("profile") or {}
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "real_name": raw.get("real_name") or profile.get("real_name", ""),
        "display_name": profile.get("display_name", ""),
        "email": profile.get("email", ""),
        "is_bot": bool(raw.get("is_bot")),
        "deleted": bool(raw.get("deleted")),
    }


def channel_entry(raw: dict) -> dict:
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "is_private": bool(raw.get("is_private")),
        "is_archived": bool(raw.get("is_archived")),
        "is_member": bool(raw.get("is_member")),
        "num_members": int(raw.get("num_members") or 0),
        "topic": ((raw.get("topic") or {}).get("value") or "")[:200],
        "purpose": ((raw.get("purpose") or {}).get("value") or "")[:200],
    }


def display_name(entry: dict | None) -> str:
    """The name to show for a user entry, neutralized for prompts."""
    if not entry:
        return ""
    return slack_text.safe_name(
        entry.get("display_name") or entry.get("real_name") or entry.get("name") or ""
    )


def find_users(data: dict, query: str) -> list[dict]:
    needle = query.lower()
    return [
        entry
        for entry in data["users"]["items"].values()
        if any(needle in str(entry.get(field) or "").lower() for field in USER_SEARCH_FIELDS)
    ]


def find_channels(data: dict, query: str) -> list[dict]:
    needle = query.lower().lstrip("#")
    return [
        entry
        for entry in data["channels"]["items"].values()
        if needle in entry["id"].lower() or needle in entry["name"].lower()
    ]


# -- Single-entry updates (Slack events, users.info, conversations.info) --


def put_user(paths: Paths, raw: dict) -> None:
    if not raw.get("id"):
        return
    data = load(paths)
    data["users"]["items"][raw["id"]] = user_entry(raw)
    save(paths, data)


def put_channel(paths: Paths, raw: dict) -> None:
    """Upsert a channel; a partial event (``channel_rename`` sends id and name) keeps the rest."""
    if not raw.get("id"):
        return
    data = load(paths)
    items = data["channels"]["items"]
    fresh = channel_entry(raw)
    existing = items.get(raw["id"])
    if existing is not None:
        items[raw["id"]] = {**existing, **{k: v for k, v in fresh.items() if k in raw}}
    elif fresh["name"]:
        items[raw["id"]] = fresh
    else:
        return
    save(paths, data)


def drop_channel(paths: Paths, channel_id: str) -> None:
    data = load(paths)
    if data["channels"]["items"].pop(channel_id, None) is not None:
        save(paths, data)


# -- Slack calls --


async def _paginate(client: AsyncWebClient, method: str, key: str, **params: Any) -> list[dict]:
    items: list[dict] = []
    cursor = None
    while True:
        response = await getattr(client, method)(**params, cursor=cursor, limit=PAGE_SIZE)
        items.extend(response.get(key) or [])
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return items


async def refresh_users(paths: Paths, client: AsyncWebClient) -> dict:
    members = await _paginate(client, "users_list", "members")
    data = await asyncio.to_thread(load, paths)
    data["users"] = {
        "fetched_at": time.time(),
        "items": {raw["id"]: user_entry(raw) for raw in members if raw.get("id")},
    }
    await asyncio.to_thread(save, paths, data)
    log.info("slack cache: %d users", len(data["users"]["items"]))
    return data


async def refresh_channels(paths: Paths, client: AsyncWebClient) -> dict:
    channels = await _paginate(
        client,
        "conversations_list",
        "channels",
        types="public_channel,private_channel",
        exclude_archived=True,
    )
    data = await asyncio.to_thread(load, paths)
    data["channels"] = {
        "fetched_at": time.time(),
        "items": {raw["id"]: channel_entry(raw) for raw in channels if raw.get("id")},
    }
    await asyncio.to_thread(save, paths, data)
    log.info("slack cache: %d channels", len(data["channels"]["items"]))
    return data


def _stale(section: dict) -> bool:
    return time.time() - float(section.get("fetched_at") or 0) >= MIN_REFRESH_SECONDS


async def lookup_users(paths: Paths, client: AsyncWebClient, query: str) -> list[dict]:
    """Every matching user; a miss refreshes the roster once per minute at most."""
    data = await asyncio.to_thread(load, paths)
    found = find_users(data, query)
    if found or not _stale(data["users"]):
        return found
    return find_users(await refresh_users(paths, client), query)


async def lookup_channels(paths: Paths, client: AsyncWebClient, query: str) -> list[dict]:
    data = await asyncio.to_thread(load, paths)
    found = find_channels(data, query)
    if found or not _stale(data["channels"]):
        return found
    return find_channels(await refresh_channels(paths, client), query)


async def whois(paths: Paths, client: AsyncWebClient, user_id: str) -> dict | None:
    """One user's entry, fetched with ``users.info`` and cached on a miss."""
    data = await asyncio.to_thread(load, paths)
    entry = data["users"]["items"].get(user_id)
    if entry is not None:
        return entry
    try:
        raw = (await client.users_info(user=user_id)).get("user")
    except Exception as exc:
        log.debug("users.info %s failed: %s", user_id, exc)
        return None
    if not raw:
        return None
    await asyncio.to_thread(put_user, paths, raw)
    return user_entry(raw)


async def channel_info(paths: Paths, client: AsyncWebClient, channel_id: str) -> dict | None:
    """One channel's entry, fetched with ``conversations.info`` and cached when it has a name."""
    data = await asyncio.to_thread(load, paths)
    entry = data["channels"]["items"].get(channel_id)
    if entry is not None:
        return entry
    try:
        raw = (await client.conversations_info(channel=channel_id)).get("channel")
    except Exception as exc:
        log.debug("conversations.info %s failed: %s", channel_id, exc)
        return None
    if not raw:
        return None
    await asyncio.to_thread(put_channel, paths, raw)  # a DM has no name and is not kept
    return channel_entry(raw)


async def names(paths: Paths, client: AsyncWebClient, user_ids: Iterable[str]) -> dict[str, str]:
    """Display names for rendering messages; unknown ids are fetched once."""
    return {user_id: display_name(await whois(paths, client, user_id)) for user_id in set(user_ids)}


async def open_dm(paths: Paths, client: AsyncWebClient, user_id: str) -> str:
    """The DM channel id for a user, opened once and remembered."""
    data = await asyncio.to_thread(load, paths)
    if channel := data["dms"].get(user_id):
        return str(channel)
    channel = (await client.conversations_open(users=user_id))["channel"]["id"]
    data = await asyncio.to_thread(load, paths)
    data["dms"][user_id] = channel
    await asyncio.to_thread(save, paths, data)
    return str(channel)
