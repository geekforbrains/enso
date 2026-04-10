#!/usr/bin/env python3
"""Slack search and history tool for Enso agents.

Usage:
    python slack_search.py search "query" [--count N]
    python slack_search.py history <channel_id> [--count N]
    python slack_search.py thread <channel_id> <thread_ts>
    python slack_search.py channels
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def _load_token() -> str:
    """Load Slack bot token from Enso config."""
    config_path = os.path.expanduser("~/.enso/config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        token = config.get("transports", {}).get("slack", {}).get("bot_token", "")
        if not token:
            print("Error: No Slack bot token in ~/.enso/config.json", file=sys.stderr)
            sys.exit(1)
        return token
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error reading config: {exc}", file=sys.stderr)
        sys.exit(1)


def _api_call(token: str, method: str, params: dict | None = None) -> dict:
    """Make a Slack Web API call."""
    url = f"https://slack.com/api/{method}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        print(f"Slack API error: {data.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    return data


def _api_post(token: str, method: str, body: dict) -> dict:
    """Make a Slack Web API POST call."""
    url = f"https://slack.com/api/{method}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        print(f"Slack API error: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    return result


def cmd_search(token: str, query: str, count: int = 10) -> None:
    """Search Slack messages."""
    data = _api_post(token, "search.messages", {
        "query": query, "count": count, "sort": "timestamp", "sort_dir": "desc",
    })
    matches = data.get("messages", {}).get("matches", [])
    if not matches:
        print("No results found.")
        return

    for msg in matches:
        channel = msg.get("channel", {}).get("name", "?")
        user = msg.get("username", msg.get("user", "?"))
        ts = msg.get("ts", "?")
        text = msg.get("text", "")[:200]
        permalink = msg.get("permalink", "")
        print(f"#{channel} | {user} | {ts}")
        print(f"  {text}")
        if permalink:
            print(f"  {permalink}")
        print()


def cmd_history(token: str, channel: str, count: int = 10) -> None:
    """Get recent channel messages."""
    data = _api_call(token, "conversations.history", {
        "channel": channel, "limit": str(count),
    })
    messages = data.get("messages", [])
    messages.reverse()  # chronological order

    for msg in messages:
        user = msg.get("user", "bot")
        text = msg.get("text", "")
        ts = msg.get("ts", "?")
        thread = f" [thread: {msg['thread_ts']}]" if msg.get("thread_ts") else ""
        print(f"{ts} | {user}{thread}")
        print(f"  {text}")
        print()


def cmd_thread(token: str, channel: str, thread_ts: str) -> None:
    """Get all messages in a thread."""
    data = _api_call(token, "conversations.replies", {
        "channel": channel, "ts": thread_ts, "limit": "100",
    })
    messages = data.get("messages", [])

    for msg in messages:
        user = msg.get("user", "bot")
        text = msg.get("text", "")
        ts = msg.get("ts", "?")
        print(f"{ts} | {user}")
        print(f"  {text}")
        print()


def cmd_channels(token: str) -> None:
    """List channels the bot has access to."""
    data = _api_call(token, "conversations.list", {
        "types": "public_channel,private_channel", "limit": "200",
    })
    channels = data.get("channels", [])

    for ch in channels:
        cid = ch.get("id", "?")
        name = ch.get("name", "?")
        topic = ch.get("topic", {}).get("value", "")[:60]
        member = "member" if ch.get("is_member") else ""
        print(f"{cid}  #{name}  {member}  {topic}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Slack search tool for Enso")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search messages")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--count", type=int, default=10)

    p_history = sub.add_parser("history", help="Channel history")
    p_history.add_argument("channel", help="Channel ID")
    p_history.add_argument("--count", type=int, default=10)

    p_thread = sub.add_parser("thread", help="Thread messages")
    p_thread.add_argument("channel", help="Channel ID")
    p_thread.add_argument("thread_ts", help="Thread timestamp")

    sub.add_parser("channels", help="List channels")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    token = _load_token()

    if args.command == "search":
        cmd_search(token, args.query, args.count)
    elif args.command == "history":
        cmd_history(token, args.channel, args.count)
    elif args.command == "thread":
        cmd_thread(token, args.channel, args.thread_ts)
    elif args.command == "channels":
        cmd_channels(token)


if __name__ == "__main__":
    main()
