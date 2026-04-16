"""Slack directory cache — users and channels stored as JSON on disk.

The same file is read and written by two callers:

- The Slack transport (``enso.transports.slack``) updates entries in
  response to Socket Mode events like ``user_change`` or
  ``channel_archived``.
- The ``enso slack`` CLI commands refresh the cache on demand via
  ``users.list`` / ``conversations.list`` and answer lookup queries.

Writes go through :func:`save` which uses ``os.replace`` for atomicity, so
last-write-wins is the only concurrency behaviour callers need to worry
about. At typical workspace scale (low hundreds of users, low hundreds of
channels) the file is well under 1 MB and loads/saves are sub-millisecond.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from .config import CONFIG_DIR

log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "slack.json")

# Slack Web API base URL.
API = "https://slack.com/api/"

# Minimum seconds between automatic refreshes of the same list to avoid
# hammering the API if the agent spams lookups for non-existent names.
MIN_REFRESH_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Cache file I/O
# ---------------------------------------------------------------------------


def _empty_cache() -> dict[str, Any]:
    """Return a fresh empty-cache structure."""
    return {
        "team_id": "",
        "users": {"fetched_at": 0.0, "items": {}},
        "channels": {"fetched_at": 0.0, "items": {}},
        "dm_cache": {},
    }


def load() -> dict[str, Any]:
    """Load the cache from disk. Returns a fresh empty structure on miss."""
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty_cache()
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Corrupt Slack cache at %s (%s) — starting empty", CACHE_FILE, exc)
        return _empty_cache()
    # Be tolerant of older shapes — fill in any missing top-level keys.
    default = _empty_cache()
    for key, value in default.items():
        data.setdefault(key, value)
    return data


def save(data: dict[str, Any]) -> None:
    """Atomic write of the cache dict."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, CACHE_FILE)


# ---------------------------------------------------------------------------
# Slack API helpers (stdlib only)
# ---------------------------------------------------------------------------


def _get(token: str, method: str, params: dict[str, str] | None = None) -> dict:
    """Call a GET-style Slack API method. Raises on transport failure."""
    url = API + method
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _post(token: str, method: str, body: dict) -> dict:
    """Call a POST-style Slack API method with a JSON body."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API + method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Entry normalisation
# ---------------------------------------------------------------------------


def _normalise_user(raw: dict) -> dict:
    """Trim a Slack user object to just the fields we care about."""
    profile = raw.get("profile", {}) or {}
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "real_name": raw.get("real_name") or profile.get("real_name", ""),
        "display_name": profile.get("display_name", ""),
        "email": profile.get("email", ""),
        "is_bot": bool(raw.get("is_bot", False)),
        "deleted": bool(raw.get("deleted", False)),
    }


def _normalise_channel(raw: dict) -> dict:
    """Trim a Slack conversation object to just the fields we care about."""
    topic = (raw.get("topic") or {}).get("value", "")
    purpose = (raw.get("purpose") or {}).get("value", "")
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "is_private": bool(raw.get("is_private", False)),
        "is_archived": bool(raw.get("is_archived", False)),
        "is_member": bool(raw.get("is_member", False)),
        "num_members": int(raw.get("num_members", 0) or 0),
        "topic": topic[:200],
        "purpose": purpose[:200],
    }


# ---------------------------------------------------------------------------
# Refreshes
# ---------------------------------------------------------------------------


def _paginate(token: str, method: str, params: dict[str, str], key: str) -> list[dict]:
    """Iterate through a cursor-paginated Slack list endpoint."""
    cursor = ""
    items: list[dict] = []
    while True:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        data = _get(token, method, page_params)
        if not data.get("ok"):
            err = data.get("error", "unknown")
            raise RuntimeError(f"{method}: {err}")
        items.extend(data.get(key, []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    return items


def refresh_users(token: str, cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch the whole user roster. Returns the updated cache (saved to disk)."""
    cache = cache if cache is not None else load()
    raw_users = _paginate(token, "users.list", {"limit": "200"}, "members")
    items = {u["id"]: _normalise_user(u) for u in raw_users if u.get("id")}
    cache["users"] = {"fetched_at": time.time(), "items": items}
    save(cache)
    log.info("Slack cache: refreshed %d users", len(items))
    return cache


def refresh_channels(token: str, cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch channels (public + private, excluding archived)."""
    cache = cache if cache is not None else load()
    params = {
        "types": "public_channel,private_channel",
        "exclude_archived": "true",
        "limit": "200",
    }
    raw = _paginate(token, "conversations.list", params, "channels")
    items = {c["id"]: _normalise_channel(c) for c in raw if c.get("id")}
    cache["channels"] = {"fetched_at": time.time(), "items": items}
    save(cache)
    log.info("Slack cache: refreshed %d channels", len(items))
    return cache


def _recently_refreshed(section: dict) -> bool:
    """Return True if ``section`` was refreshed within the guard window."""
    return (time.time() - float(section.get("fetched_at", 0) or 0)
            < MIN_REFRESH_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _match_user(entry: dict, query: str) -> bool:
    q = query.lower()
    for field in ("id", "name", "real_name", "display_name", "email"):
        if q in (entry.get(field) or "").lower():
            return True
    return False


def _match_channel(entry: dict, query: str) -> bool:
    q = query.lower().lstrip("#")
    return q in (entry.get("id") or "").lower() or q in (entry.get("name") or "").lower()


def lookup_user(
    query: str,
    *,
    token: str | None = None,
    refresh_on_miss: bool = True,
) -> list[dict]:
    """Search the user cache. If ``token`` is given and nothing matches,
    refresh once (subject to the min-interval guard) and retry.
    Returns **all** matches — the caller decides how to disambiguate.
    """
    cache = load()
    matches = [u for u in cache["users"]["items"].values() if _match_user(u, query)]
    if matches or not token or not refresh_on_miss:
        return matches
    if _recently_refreshed(cache["users"]):
        return []
    cache = refresh_users(token, cache)
    return [u for u in cache["users"]["items"].values() if _match_user(u, query)]


def lookup_channel(
    query: str,
    *,
    token: str | None = None,
    refresh_on_miss: bool = True,
) -> list[dict]:
    """Search the channel cache with the same refresh-on-miss behaviour."""
    cache = load()
    matches = [c for c in cache["channels"]["items"].values() if _match_channel(c, query)]
    if matches or not token or not refresh_on_miss:
        return matches
    if _recently_refreshed(cache["channels"]):
        return []
    cache = refresh_channels(token, cache)
    return [c for c in cache["channels"]["items"].values() if _match_channel(c, query)]


def whois(user_id: str, *, token: str | None = None) -> dict | None:
    """Resolve a ``U…`` ID to a user record. Hits ``users.info`` on miss."""
    cache = load()
    entry = cache["users"]["items"].get(user_id)
    if entry or not token:
        return entry
    data = _get(token, "users.info", {"user": user_id})
    if not data.get("ok") or not data.get("user"):
        return None
    user = _normalise_user(data["user"])
    cache["users"]["items"][user_id] = user
    save(cache)
    return user


def open_dm(user_id: str, token: str) -> str:
    """Open a DM with a user and cache the resulting channel ID."""
    cache = load()
    dm = cache.get("dm_cache", {}).get(user_id)
    if dm:
        return dm
    data = _post(token, "conversations.open", {"users": user_id})
    if not data.get("ok"):
        raise RuntimeError(f"conversations.open: {data.get('error', 'unknown')}")
    channel_id = data["channel"]["id"]
    cache.setdefault("dm_cache", {})[user_id] = channel_id
    save(cache)
    return channel_id


# ---------------------------------------------------------------------------
# Event-driven mutations (called from the Slack transport)
# ---------------------------------------------------------------------------


def apply_user_change(raw_user: dict) -> None:
    """Handle ``user_change`` / ``team_join`` events."""
    if not raw_user.get("id"):
        return
    cache = load()
    cache["users"]["items"][raw_user["id"]] = _normalise_user(raw_user)
    save(cache)


def apply_channel_upsert(raw_channel: dict) -> None:
    """Handle ``channel_created`` / ``channel_rename`` / ``channel_archived`` /
    ``channel_unarchived`` — anything that writes a channel record."""
    if not raw_channel.get("id"):
        return
    cache = load()
    existing = cache["channels"]["items"].get(raw_channel["id"], {})
    merged = {**existing, **_normalise_channel(raw_channel)}
    cache["channels"]["items"][raw_channel["id"]] = merged
    save(cache)


def apply_channel_delete(channel_id: str) -> None:
    """Handle ``channel_deleted``."""
    if not channel_id:
        return
    cache = load()
    cache["channels"]["items"].pop(channel_id, None)
    save(cache)


def set_channel_is_member(channel_id: str, is_member: bool) -> None:
    """Flip the ``is_member`` flag on a channel (``member_joined_channel`` or
    ``member_left_channel`` where the subject is the bot itself)."""
    if not channel_id:
        return
    cache = load()
    entry = cache["channels"]["items"].get(channel_id)
    if not entry:
        return
    entry["is_member"] = bool(is_member)
    save(cache)
