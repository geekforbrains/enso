---
name: slack
description: Look up Slack users and channels by name, open DMs, read channel history, and search messages. Use when you need to resolve a name to an ID (e.g. someone asks you to "mention Alex" and you need `<@U…>`), find a channel, or retrieve context from other Slack conversations.
---

# Slack

The `enso slack` CLI is the entry point for Slack directory lookup, search, and history. It's always available — no token management on your side. Follow the active Slack turn's advertised rich-output or surface contract when it is present; never send those envelopes through `enso message send` or `enso message attach`, and never invent destinations, recipients, access levels, or action IDs. Those CLI commands remain text/caption-only delivery paths.

## Name ↔ ID lookups

Whenever someone says "mention Alex" or "post to #general", resolve the name to an ID before composing the message:

```bash
enso slack lookup-user "alex"           # by name / real_name / display / email
enso slack lookup-channel "general"        # by name or channel ID
enso slack whois U0123456789             # reverse: ID → user record
```

- Output is one line per match: `<id>  <real_name>  (@handle)  <email>  [tags]`
- Multiple matches means the name was ambiguous — ask the human to clarify before picking.
- The CLI caches results locally and refreshes automatically on a miss, so these calls are effectively free after the first lookup.

### Using the IDs in messages

Once you have an ID, embed it in your Slack message using Slack mention syntax:

- User mention: `<@U0123456789>`
- Channel reference: `<#C0123456789|general>`

Slack renders these as clickable mentions/links automatically.

## Opening a DM

To send someone a direct message, you need the DM channel ID (`D…`), not their user ID:

```bash
enso slack open-dm U0123456789           # or by name: open-dm "alex"
# → prints D0AFV5ANEGY
```

Feed that channel ID to `enso message send --to <D…>` or `enso message attach --to <D…>`.

## Listing

```bash
enso slack list users                    # every cached user
enso slack list channels                 # every cached channel
enso slack refresh                       # force-refresh both
enso slack refresh --users               # or just one
```

If the cache is empty the list commands refresh automatically.

## Message search & history

No cache here — these hit the API live each time:

```bash
enso slack search "deploy failed"        # search.messages
enso slack search "from:@alex report"
enso slack history C0123456789 --count 30
enso slack thread C0123456789 1706789234.123456
```

`enso slack search` uses the bot token with the `search:read.public` scope the bundled app manifest grants — it searches public channels only. On apps created before that scope was added, it errors with `missing_scope`/`not_allowed_token_type` until the app is updated from the current manifest and reinstalled. `history` and `thread` work with any standard bot token.

## Notes

- All commands use the bot token from `~/.enso/config.json` automatically.
- Channel IDs start with `C` (public), `G` (private group), `D` (DM).
- User IDs start with `U` (regular) or `W` (Enterprise Grid).
- The cache lives at `~/.enso/cache/slack.json` and is kept fresh both by these CLI commands and by real-time Slack events when the bot is running.
