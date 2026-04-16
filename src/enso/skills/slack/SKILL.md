---
name: slack
description: Look up Slack users and channels by name, open DMs, read channel history, and search messages. Use when you need to resolve a name to an ID (e.g. someone asks you to "mention Gavin" and you need `<@U…>`), find a channel, or retrieve context from other Slack conversations.
---

# Slack

The `enso slack` CLI is the single entry point for anything Slack-related.
It's always available — no token management on your side.

## Name ↔ ID lookups

Whenever someone says "mention Gavin" or "post to #daily", resolve the
name to an ID before composing the message:

```bash
enso slack lookup-user "gavin"           # by name / real_name / display / email
enso slack lookup-channel "daily"        # by name or channel ID
enso slack whois U0AETSSDDEF             # reverse: ID → user record
```

- Output is one line per match: `<id>  <real_name>  (@handle)  <email>  [tags]`
- Multiple matches means the name was ambiguous — ask the human to clarify
  before picking.
- The CLI caches results locally and refreshes automatically on a miss, so
  these calls are effectively free after the first lookup.

### Using the IDs in messages

Once you have an ID, embed it in your Slack message using Slack mention
syntax:

- User mention: `<@U0AETSSDDEF>`
- Channel reference: `<#C0AEWRPJ9LM|daily>`

Slack renders these as clickable mentions/links automatically.

## Opening a DM

To send someone a direct message, you need the DM channel ID (`D…`), not
their user ID:

```bash
enso slack open-dm U0AETSSDDEF           # or by name: open-dm "gavin"
# → prints D0AFV5ANEGY
```

Feed that channel ID to `enso message send --to <D…>` or
`enso message attach --to <D…>`.

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
enso slack search "from:@gavin report"
enso slack history C0AEWRPJ9LM --count 30
enso slack thread C0AEWRPJ9LM 1706789234.123456
```

`enso slack search` requires a user-token scope (`search:read`) and may
error with `not_allowed_token_type` on bot-only installations. `history`
and `thread` work with a standard bot token.

## Notes

- All commands use the bot token from `~/.enso/config.json` automatically.
- Channel IDs start with `C` (public), `G` (private group), `D` (DM).
- User IDs start with `U` (regular) or `W` (Enterprise Grid).
- The cache lives at `~/.enso/cache/slack.json` and is kept fresh both
  by these CLI commands and by real-time Slack events when the bot is
  running.
