# Task 09: Tests for Slack Transport

**Status:** Done
**Priority:** Medium
**Depends on:** Task 02, Task 03

## Description

Add unit tests for the Slack transport, mirroring the existing test patterns.

## Test Files

- `tests/test_slack_transport.py` — test SlackContext and SlackTransport

## Test Cases

### SlackContext
- `test_reply_posts_message` — verify `chat_postMessage` is called with correct channel/thread
- `test_reply_splits_long_messages` — messages over 4000 chars are chunked
- `test_reply_status_returns_ts` — status message returns a `ts` handle
- `test_edit_status_calls_update` — verify `chat_update` with correct `ts`
- `test_delete_status_calls_delete` — verify `chat_delete`
- `test_delete_status_suppresses_errors` — errors don't propagate

### SlackTransport
- `test_unauthorized_user_rejected` — messages from non-allowed users are ignored
- `test_message_dispatched_to_runtime` — DM messages reach `runtime.process_request()`
- `test_concurrent_request_rejected` — second message while locked gets rejection reply
- `test_command_parsing` — `!use claude`, `!stop`, etc. are parsed correctly
- `test_notify_sends_dm` — notifications are sent to all allowed users

### Integration
- Manual testing guide: connect to a test Slack workspace, send messages, verify responses

## Acceptance Criteria

- [ ] Unit tests pass with mocked Slack client
- [ ] Tests cover happy path and error cases
- [ ] Tests run in CI (no Slack credentials needed — all mocked)
