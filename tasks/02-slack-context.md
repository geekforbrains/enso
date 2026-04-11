# Task 02: Implement SlackContext (TransportContext)

**Status:** Done
**Priority:** High
**Depends on:** Task 01

## Description

Implement `SlackContext` — the per-message interface that the runtime uses to send responses back to Slack. This maps Enso's abstract methods to Slack Web API calls.

## Interface Mapping

| TransportContext method | Slack API call | Notes |
|---|---|---|
| `reply(text)` | `client.chat_postMessage(channel, text, thread_ts)` | Final response. Split at 4000 chars (Slack limit). Use mrkdwn formatting. |
| `reply_status(text)` | `client.chat_postMessage(channel, text, thread_ts)` | Returns message `ts` as the handle for later edits. |
| `edit_status(handle, text)` | `client.chat_update(channel, ts=handle, text)` | Update the status message in-place. |
| `delete_status(handle)` | `client.chat_delete(channel, ts=handle)` | Clean up status message. |
| `send_typing()` | No direct equivalent in Slack — can be a no-op, or use `chat_postMessage` with a typing indicator block if desired. | Slack doesn't have a persistent typing indicator API for bots. |

## Key Decisions

- **Threading**: All replies should go to the same thread as the incoming message. Store `thread_ts` (or the message's own `ts` if it's a top-level message that should start a thread).
- **Formatting**: Slack uses mrkdwn (not HTML). Need a `md_to_slack()` formatter or pass raw markdown (Slack's mrkdwn is close enough for most cases).
- **Message length**: Slack's limit is 4000 chars per message (vs Telegram's 4096). The runtime's `split_text()` already accepts a `limit` parameter.

## File

`src/enso/transports/slack.py` (new file)

## Acceptance Criteria

- [ ] `SlackContext` implements all 5 `TransportContext` methods
- [ ] Replies are threaded correctly
- [ ] Messages over 4000 chars are split
- [ ] Status messages can be edited and deleted via Slack `ts` handles
