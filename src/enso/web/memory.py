"""Read-only episodic Memory pages, grouped by event time in the configured timezone."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import tzinfo
from typing import Any
from urllib.parse import quote, urlencode

from .. import memory
from ..config import Paths
from . import common, files, filters

PAGE_SIZE = 50
SOURCE_PREVIEW = 16_000
DATE_FILTERS = (
    ("", "Any time"),
    ("today", "Today"),
    ("week", "This week"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
)


def browse_url(**values: str | int) -> str:
    """Keep filters in ordinary links, including names containing spaces or punctuation."""
    query = urlencode({key: value for key, value in values.items() if value != ""})
    return "/memory" + ("?" + query if query else "")


def _timestamp(stamp: str | None, zone: tzinfo | None) -> dict[str, str]:
    moment = filters.parse_time(stamp)
    if moment is None:
        return {"iso": "", "full": "Unknown", "clock": "—", "date": "Unknown date"}
    local = moment.astimezone(zone)
    return {
        "iso": local.isoformat(timespec="seconds"),
        "full": local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "clock": local.strftime("%H:%M"),
        "date": local.strftime("%A, %B %-d, %Y"),
    }


def _context(paths: Paths) -> tuple[dict[str, Any], tzinfo | None]:
    config, problems = common.read_config(paths)
    timezone = config.memory.timezone if config else "local"
    zone = memory.timezone_info(timezone)
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "timezone": timezone,
        "timezone_label": str(zone) if zone else "Local time",
        "enabled": config.memory.enabled if config else True,
    }, zone


def listing_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any]:
    """One bounded page; invalid filters and unreadable databases stay visible as errors."""
    context, zone = _context(paths)
    values = {
        key: query.get(key, "").strip()[:200]
        for key in ("workspace", "transport", "channel", "since", "until", "q")
    }
    raw_page = query.get("page", "1")
    page = max(1, int(raw_page)) if raw_page.isdecimal() and len(raw_page) < 10 else 1
    facets, facet_error = common.attempt(lambda: memory.facets(paths))

    def read() -> memory.MemoryPage:
        return memory.list_entries(
            paths,
            workspace=values["workspace"] or None,
            transport=values["transport"] or None,
            channel=values["channel"] or None,
            since=memory.parse_since(values["since"], context["timezone"]),
            until=memory.parse_since(values["until"], context["timezone"]),
            query=values["q"],
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )

    result, error = common.attempt(read)
    total = result.total if result else 0
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if result and page > pages:
        page = pages
        result, error = common.attempt(read)
    entries = result.entries if result else ()
    groups: list[dict[str, Any]] = []
    for entry in entries:
        stamp = _timestamp(entry.occurred_at, zone)
        if not groups or groups[-1]["label"] != stamp["date"]:
            groups.append({"label": stamp["date"], "rows": []})
        groups[-1]["rows"].append(
            {
                "entry": entry,
                "stamp": stamp,
                "preview": " ".join(entry.summary.split()),
                "href": "/memory/" + quote(entry.ref, safe=""),
            }
        )
    date_filters = list(DATE_FILTERS)
    if values["since"] not in dict(DATE_FILTERS):
        date_filters.append((values["since"], "Since " + values["since"]))
    choices = facets or {"workspaces": [], "transports": [], "channels": []}
    # A bookmarked filter remains selected even after its last matching entry is forgotten.
    choices = {
        key + "s": sorted(set(choices[key + "s"]) | ({values[key]} if values[key] else set()))
        for key in ("workspace", "transport", "channel")
    }
    return {
        **context,
        **values,
        "error": error or facet_error,
        "facets": choices,
        "channel_names": (facets or {}).get("channel_names", {}),
        "date_filters": date_filters,
        "filtered": any(values.values()),
        "groups": groups,
        "total": total,
        "page": page,
        "pages": pages,
        "start": (page - 1) * PAGE_SIZE + 1 if entries else 0,
        "end": (page - 1) * PAGE_SIZE + len(entries) if entries else 0,
        "prev_link": browse_url(**values, page=page - 1) if page > 1 else None,
        "next_link": browse_url(**values, page=page + 1) if page < pages else None,
    }


def entry_model(paths: Paths, ref: str) -> dict[str, Any] | None:
    """One summary and its bounded clean exchanges; viewing never processes or edits them."""
    context, zone = _context(paths)
    entry, error = common.attempt(lambda: memory.get_entry(paths, ref))
    if entry is None:
        return {**context, "entry": None, "error": error} if error else None
    exchanges, source_error = common.attempt(lambda: memory.sources(paths, ref))
    sources = []
    for turn in exchanges or ():
        request, response = turn.request[:SOURCE_PREVIEW], turn.response[:SOURCE_PREVIEW]
        sources.append(
            {
                "turn": turn,
                "received": _timestamp(turn.received_at, zone),
                "completed": _timestamp(turn.completed_at, zone),
                "request_html": files.render_output(request),
                "response_html": files.render_output(response),
                "request_clipped": len(turn.request) > SOURCE_PREVIEW,
                "response_clipped": len(turn.response) > SOURCE_PREVIEW,
            }
        )
    return {
        **context,
        "entry": entry,
        "error": source_error,
        "html": files.render_markdown(entry.summary),
        "occurred": _timestamp(entry.occurred_at, zone),
        "ended": _timestamp(entry.ended_at, zone),
        "created": _timestamp(entry.created_at, zone),
        "sources": sources,
        "workspace_url": browse_url(workspace=entry.workspace),
    }
