"""``enso slack``: writes, reads, and directory lookups over ``slack_sdk``."""

from __future__ import annotations

import textwrap
import time
from collections.abc import Callable
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .. import slack_cache, slack_text
from ..config import Config, Paths
from ..outbound import FENCE, EnvelopeError, OutboundMessage, parse_outbound_message
from .common import (
    ACTION_KEY,
    JSON_FLAG,
    body,
    deliver,
    echo_json,
    fail,
    load,
    parse_duration,
    report,
    run,
    written,
)

if TYPE_CHECKING:
    from ..transports.slack import SlackTransport

slack_app = typer.Typer(no_args_is_help=True, help="Talk to Slack from a shell or a job.")

CHANNEL = typer.Option(..., "-c", "--channel", help="Conversation id: C…, G…, or D….")
THREAD = typer.Option(None, "-t", "--thread", help="Post into this thread (its root ts).")
TS = typer.Option(..., "--ts", help="The message's ts.")
FILE = typer.Option(None, "--file", help="Read the text from this file.")
SHOW_ALL = typer.Option(False, "--all", help="Include joins, pins, and other lifecycle noise.")
RICH = typer.Option(None, "--rich", help="Post this enso-message envelope file.")
UPLOAD = typer.Argument(..., help="The file to upload.")


@slack_app.command("manifest")
def slack_manifest() -> None:
    """Print the packaged Slack app manifest as JSON; no config or Slack extra required."""
    typer.echo(
        resources.files("enso").joinpath("bundled/slack/manifest.json").read_text("utf-8"),
        nl=False,
    )


def transport(config: Config, *, as_json: bool) -> SlackTransport:
    """An unconnected Slack transport; its client is enough for one-off API calls."""
    if config.slack is None:
        fail(["transports.slack is not configured"], as_json=as_json)
    try:
        from ..transports.slack import SlackTransport
    except ImportError as exc:
        fail([str(exc)], as_json=as_json)
    return SlackTransport(config.slack, config.paths)


def _open(paths: Paths, *, as_json: bool) -> SlackTransport:
    return transport(load(paths, as_json=as_json), as_json=as_json)


def _envelope(content: str) -> OutboundMessage:
    """An ``enso-message`` file holds either the fenced form or the bare JSON."""
    if FENCE not in content:
        content = f"{FENCE}\n{content.strip()}\n```"
    parsed = parse_outbound_message(content)
    assert parsed is not None  # a fence is present by construction
    return parsed


# -- Writes --


@slack_app.command("send")
def slack_send(
    text: str | None = typer.Argument(None, help="Message text; '-' reads stdin."),
    channel: str = CHANNEL,
    thread: str | None = THREAD,
    file: Path | None = FILE,
    rich: Path | None = RICH,
    action_key: str | None = ACTION_KEY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Post Markdown text, or a table/chart envelope with --rich."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)
    envelope = None
    if rich is not None:
        if text is not None or file is not None:
            fail(["--rich replaces TEXT and --file"], as_json=as_json)
        try:
            envelope = _envelope(rich.read_text(encoding="utf-8"))
        except (OSError, EnvelopeError) as exc:
            fail([f"{rich}: {exc}"], as_json=as_json)
        content = envelope.fallback_text
    else:
        content = body(text, file, as_json=as_json)
    result = run(
        deliver(paths, slack, channel, thread, text=content, rich=envelope, action_key=action_key),
        as_json=as_json,
    )
    report(result, as_json=as_json)


@slack_app.command("upload")
def slack_upload(
    file: Path = UPLOAD,
    channel: str = CHANNEL,
    thread: str | None = THREAD,
    caption: str = typer.Option("", "--caption", help="Text posted with the file."),
    action_key: str | None = ACTION_KEY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Upload a file, optionally with a caption."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)
    if not file.is_file():
        fail([f"{file} is not a file"], as_json=as_json)
    result = run(
        deliver(paths, slack, channel, thread, file=file, caption=caption, action_key=action_key),
        as_json=as_json,
    )
    report(result, as_json=as_json)


@slack_app.command("edit")
def slack_edit(
    text: str | None = typer.Argument(None, help="New text; '-' reads stdin."),
    channel: str = CHANNEL,
    ts: str = TS,
    file: Path | None = FILE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Replace a message's text."""
    slack = _open(Paths.from_env(), as_json=as_json)
    content = body(text, file, as_json=as_json)

    async def go() -> dict:
        await slack.edit(channel, ts, content)
        return await written(slack, channel, ts, None, file=False)

    report(run(go(), as_json=as_json), as_json=as_json)


@slack_app.command("delete")
def slack_delete(channel: str = CHANNEL, ts: str = TS, as_json: bool = JSON_FLAG) -> None:
    """Delete a message."""
    slack = _open(Paths.from_env(), as_json=as_json)

    async def go() -> dict:
        await slack.delete(channel, ts)
        return {"ok": True, "transport": "slack", "channel": channel, "ts": ts}

    report(run(go(), as_json=as_json), as_json=as_json)


@slack_app.command("react")
def slack_react(
    emoji: str = typer.Argument(..., help="Emoji name, e.g. white_check_mark."),
    channel: str = CHANNEL,
    ts: str = TS,
    as_json: bool = JSON_FLAG,
) -> None:
    """Add a reaction to a message."""
    slack = _open(Paths.from_env(), as_json=as_json)
    name = emoji.strip(":")

    async def go() -> dict:
        await slack.client.reactions_add(channel=channel, timestamp=ts, name=name)
        return {"ok": True, "transport": "slack", "channel": channel, "ts": ts, "reaction": name}

    report(run(go(), as_json=as_json), as_json=as_json)


@slack_app.command("unreact")
def slack_unreact(
    emoji: str = typer.Argument(..., help="Emoji name, e.g. white_check_mark."),
    channel: str = CHANNEL,
    ts: str = TS,
    as_json: bool = JSON_FLAG,
) -> None:
    """Remove the caller's reaction from a message."""
    slack = _open(Paths.from_env(), as_json=as_json)
    name = emoji.strip(":")

    async def go() -> dict:
        await slack.client.reactions_remove(channel=channel, timestamp=ts, name=name)
        return {"ok": True, "transport": "slack", "channel": channel, "ts": ts, "reaction": name}

    report(run(go(), as_json=as_json), as_json=as_json)


# -- Reads --


def _when(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except TypeError, ValueError:
        return "?"


async def views(slack: SlackTransport, found: list[dict], *, show_all: bool) -> list[dict]:
    """Messages as an agent should read them: names resolved, mentions inert, noise dropped."""
    kept = [m for m in found if show_all or m.get("subtype") not in slack_text.IGNORED_SUBTYPES]
    ids = {m["user"] for m in kept if m.get("user")}
    for message in kept:
        ids.update(slack_text.MENTION_RE.findall(message.get("text") or ""))
    names = await slack_cache.names(slack.paths, slack.client, ids)
    rendered = []
    for message in kept:
        text = slack_text.flatten_mentions(
            slack_text.unescape(slack_text.message_text(message)),
            bot_user_id="",
            lookup=lambda uid: names.get(uid, ""),
        )
        if not text:
            continue
        user = message.get("user") or ""
        bot = message.get("bot_profile") or {}
        view = {
            "ts": message.get("ts", ""),
            "time": _when(message.get("ts", "")),
            "user": user,
            "name": names.get(user) or bot.get("name") or message.get("username") or "",
            "text": text,
            "replies": int(message.get("reply_count") or 0),
        }
        if message.get("permalink"):
            view["permalink"] = message["permalink"]
        rendered.append(view)
    return rendered


def _print_views(found: list[dict], *, as_json: bool) -> None:
    if as_json:
        echo_json(found)
        return
    if not found:
        typer.echo("no messages")
        return
    for view in found:
        who = view["name"] or view["user"] or "unknown"
        if view["name"] and view["user"]:
            who += f" ({view['user']})"
        header = [view["time"], who]
        header.append(f"ts={view['ts']}")
        if view["replies"]:
            header.append(f"[{view['replies']} replies]")
        typer.echo("  ".join(header))
        typer.echo(textwrap.indent(view["text"], "  "))
        if view.get("permalink"):
            typer.echo(f"  {view['permalink']}")
        typer.echo("")


@slack_app.command("thread")
def slack_thread(
    channel: str,
    ts: str = typer.Argument(..., help="The thread root's ts."),
    limit: int = typer.Option(
        100, "-n", help="Keep the root plus the latest N-1 replies; 0 = all."
    ),
    show_all: bool = SHOW_ALL,
    as_json: bool = JSON_FLAG,
) -> None:
    """Print a thread, oldest first."""
    slack = _open(Paths.from_env(), as_json=as_json)

    async def go() -> list[dict]:
        return await views(slack, await slack.fetch_thread(channel, ts), show_all=show_all)

    found = run(go(), as_json=as_json)
    hidden = 0
    if 0 < limit < len(found):
        hidden = len(found) - limit
        found = found[:1] + found[len(found) - limit + 1 :] if limit > 1 else found[:1]
    _print_views(found, as_json=as_json)
    if hidden and not as_json:
        typer.echo(f"… {hidden} earlier replies not shown (raise -n to see them)", err=True)


@slack_app.command("history")
def slack_history(
    channel: str,
    since: str | None = typer.Option(None, "--since", help="Only newer than, e.g. 30m, 24h, 7d."),
    limit: int = typer.Option(20, "-n", help="How many messages at most."),
    show_all: bool = SHOW_ALL,
    as_json: bool = JSON_FLAG,
) -> None:
    """Print a channel's recent top-level messages, oldest first (replies stay in their threads)."""
    slack = _open(Paths.from_env(), as_json=as_json)
    oldest = None
    if since is not None:
        span = parse_duration(since)
        if span is None:
            fail([f"--since {since!r}: use a count and a unit, e.g. 30m, 24h, 7d"], as_json=as_json)
        oldest = f"{time.time() - span.total_seconds():.6f}"

    async def go() -> list[dict]:
        result = await slack.client.conversations_history(
            channel=channel, limit=limit, oldest=oldest
        )
        return await views(slack, list(reversed(result.get("messages") or [])), show_all=show_all)

    _print_views(run(go(), as_json=as_json), as_json=as_json)


# -- Directory --


def user_line(user: dict) -> str:
    shown = user.get("real_name") or user.get("display_name") or user.get("name") or "?"
    parts = [user["id"], shown]
    if user.get("name") and user["name"] != shown:
        parts.append(f"(@{user['name']})")
    if user.get("email"):
        parts.append(user["email"])
    tags = [tag for field, tag in (("is_bot", "bot"), ("deleted", "deleted")) if user.get(field)]
    if tags:
        parts.append(f"[{','.join(tags)}]")
    return "  ".join(parts)


def channel_line(channel: dict) -> str:
    parts = [channel["id"], f"#{channel['name']}"]
    if channel.get("is_private"):
        parts.append("[private]")
    if channel.get("is_archived"):
        parts.append("[archived]")
    if not channel.get("is_member"):
        parts.append("[not a member]")
    if channel.get("num_members"):
        parts.append(f"({channel['num_members']} members)")
    if channel.get("topic"):
        parts.append(f"— {channel['topic']}")
    return "  ".join(parts)


def _print_entries(
    entries: list[dict], line: Callable[[dict], str], *, missing: str, as_json: bool
) -> None:
    if not entries:
        fail([missing], as_json=as_json)
    if as_json:
        echo_json(entries)
        return
    for entry in entries:
        typer.echo(line(entry))


@slack_app.command("lookup-user")
def slack_lookup_user(
    query: str = typer.Argument(..., help="Part of a name, handle, email, or id."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Find users; a miss refreshes the directory cache."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)
    found = run(slack_cache.lookup_users(paths, slack.client, query), as_json=as_json)
    _print_entries(found, user_line, missing=f"no user matches {query!r}", as_json=as_json)


@slack_app.command("lookup-channel")
def slack_lookup_channel(
    query: str = typer.Argument(..., help="Part of a channel name (with or without #) or id."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Find channels; a miss refreshes the directory cache."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)
    found = run(slack_cache.lookup_channels(paths, slack.client, query), as_json=as_json)
    _print_entries(found, channel_line, missing=f"no channel matches {query!r}", as_json=as_json)


@slack_app.command("whois")
def slack_whois(user_id: str, as_json: bool = JSON_FLAG) -> None:
    """Resolve a user id."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)
    entry = run(slack_cache.whois(paths, slack.client, user_id), as_json=as_json)
    _print_entries(
        [entry] if entry else [], user_line, missing=f"no user {user_id}", as_json=as_json
    )


@slack_app.command("open-dm")
def slack_open_dm(
    user: str = typer.Argument(..., help="A user id, or a name to look up."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Open (or find) the bot's DM with a user and print its D… id."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)

    async def go() -> dict:
        user_id = user
        if not user.startswith(("U", "W")):
            found = await slack_cache.lookup_users(paths, slack.client, user)
            if len(found) != 1:
                options = "; ".join(user_line(entry) for entry in found)
                raise ValueError(
                    f"{user!r} matches {len(found)} users: {options}"
                    if found
                    else f"no user matches {user!r}"
                )
            user_id = found[0]["id"]
        channel = await slack_cache.open_dm(paths, slack.client, user_id)
        return {"ok": True, "user": user_id, "channel": channel}

    report(run(go(), as_json=as_json), as_json=as_json)


@slack_app.command("refresh")
def slack_refresh(
    users: bool = typer.Option(False, "--users", help="Only the user list."),
    channels: bool = typer.Option(False, "--channels", help="Only the channel list."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Reload the directory cache (both lists unless one is named)."""
    paths = Paths.from_env()
    slack = _open(paths, as_json=as_json)

    async def go() -> dict:
        result: dict = {"ok": True}
        if users or not channels:
            data = await slack_cache.refresh_users(paths, slack.client)
            result["users"] = len(data["users"]["items"])
        if channels or not users:
            data = await slack_cache.refresh_channels(paths, slack.client)
            result["channels"] = len(data["channels"]["items"])
        return result

    report(run(go(), as_json=as_json), as_json=as_json)
