---
name: slack
description: Search and browse Slack messages, channels, and threads. Use when you need to find information in Slack, look up what was discussed in a channel, or retrieve context from other conversations.
---

# Slack Search

You have access to a Slack search tool that can query messages across
channels and retrieve conversation history. Use it when you need context
beyond what was provided in the prompt.

## Usage

Run the helper script from the workspace:

```bash
python tools/slack_search.py <command> [options]
```

## Commands

### Search messages

Search across all channels the bot has access to:

```bash
python tools/slack_search.py search "keyword or phrase"
python tools/slack_search.py search "from:@user keyword"
python tools/slack_search.py search "in:#channel keyword"
python tools/slack_search.py search "keyword" --count 20
```

### Read channel history

Get recent messages from a specific channel:

```bash
python tools/slack_search.py history <channel_id> --count 30
```

### Read a thread

Get all messages in a specific thread:

```bash
python tools/slack_search.py thread <channel_id> <thread_ts>
```

### List channels

List channels the bot has access to:

```bash
python tools/slack_search.py channels
```

## Notes

- The bot token from `~/.enso/config.json` is used automatically.
- Search results include channel name, timestamp, user, and message text.
- Channel IDs look like `C04ABCDEF`. You can find them via the `channels` command.
- Thread timestamps look like `1706789234.123456`.
- Use `--count N` to control how many results to return (default 10).
