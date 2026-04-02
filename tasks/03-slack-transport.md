# Task 03: Implement SlackTransport (BaseTransport)

**Status:** Done
**Priority:** High
**Depends on:** Task 02

## Description

Implement `SlackTransport` — the main Slack bot that listens for messages and dispatches them to the Enso runtime. Uses Slack's Socket Mode for a persistent WebSocket connection (no public URL needed, same as Telegram polling).

## Architecture

```
Slack Bolt App (Socket Mode)
  ├── @app.message("") → _handle_message() → runtime.process_request()
  ├── @app.command("/enso-use") → _cmd_use()
  ├── @app.command("/enso-stop") → _cmd_stop()
  ├── @app.command("/enso-status") → _cmd_status()
  ├── @app.command("/enso-model") → _cmd_model()
  ├── @app.command("/enso-clear") → _cmd_clear()
  ├── @app.command("/enso-help") → _cmd_help()
  └── @app.event("app_mention") → _handle_mention() (for channel use)
```

## Key Implementation Details

### Socket Mode Setup
```python
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=bot_token)
handler = SocketModeHandler(app, app_token)
handler.start()  # blocking, like Telegram's run_polling()
```

### Message Routing
- **DMs**: All messages in DM conversations go to the agent (like Telegram)
- **Channels**: Only respond to @mentions or thread replies where the bot is already participating
- **Chat ID**: Use `channel_id` as the chat ID (maps to Enso's per-chat state)
- **Thread isolation**: Optionally use `channel_id:thread_ts` as a compound chat ID for per-thread sessions

### Authorization
- `allowed_user_ids`: List of Slack user IDs (e.g., `["U06ABCDEF"]`)
- Check `event["user"]` against the allowlist, same pattern as Telegram

### Concurrency Guard
- Use the same `runtime.get_chat_lock()` pattern to prevent concurrent requests per channel
- Reply with "A request is already running. Use /enso-stop to cancel." when locked

### Job Scheduler
- Start `runtime.run_job_scheduler()` as a background task in `start()`, same as Telegram's `_post_init()`

### Notifications
- `notify()` sends to all allowed users via DM (look up DM channel with `conversations.open`)

## Config Structure

```json
{
  "transport": "slack",
  "transports": {
    "slack": {
      "bot_token": "xoxb-...",
      "app_token": "xapp-...",
      "allowed_user_ids": ["U06ABCDEF"]
    }
  }
}
```

## Commands Approach

Two options (decide during implementation):
1. **Slack slash commands** (`/enso-use`, `/enso-stop`, etc.) — requires registering in Slack app config
2. **Message prefix commands** (`!use`, `!stop`, etc.) — parsed in `_handle_message`, no Slack config needed

Recommendation: Start with **message prefix commands** for simplicity (no Slack app manifest changes needed). Can add slash commands later.

## File

`src/enso/transports/slack.py` (same file as SlackContext)

## Acceptance Criteria

- [ ] Bot connects via Socket Mode
- [ ] DM messages are dispatched to the runtime
- [ ] @mentions in channels are dispatched to the runtime
- [ ] Authorization checks against allowed_user_ids
- [ ] Concurrent request guard works
- [ ] Job scheduler runs as a background task
- [ ] `notify()` sends DMs to allowed users
- [ ] All commands work (use, stop, status, model, clear, help)
