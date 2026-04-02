# Slack Transport for Enso — Implementation Plan

## Summary

Add a Slack transport to Enso so users can chat with their AI agent CLIs (Claude, Codex, Gemini) from Slack instead of (or in addition to) Telegram.

## Architecture

Enso's existing `BaseTransport` / `TransportContext` abstraction makes this clean:

```
Slack (Socket Mode)          Telegram (Polling)
       │                            │
  SlackTransport              TelegramTransport
       │                            │
       └────────┐    ┌──────────────┘
                │    │
              Runtime
                │
           Provider CLI
          (claude/codex/gemini)
```

No changes to the runtime or provider layers. The Slack transport is a new `BaseTransport` implementation that bridges Slack events to `runtime.process_request()`.

## Task Order (Critical Path)

```
Task 06 (dependency) ──────────────────────────────────┐
Task 01 (chat ID generalization) ─→ Task 02 (SlackContext) ─→ Task 03 (SlackTransport) ─→ Task 05 (CLI wiring)
                                    Task 04 (formatting) ──────┘                          Task 07 (setup guide)
                                                                                          Task 08 (file handling)
                                                                                          Task 09 (tests)
```

## Tasks

| # | Task | Status | Priority | Effort |
|---|---|---|---|---|
| 01 | [Chat ID Type Generalization](./01-chat-id-type-generalization.md) | **Done** | High | Small |
| 02 | [SlackContext Implementation](./02-slack-context.md) | **Done** | High | Small-Medium |
| 03 | [SlackTransport Implementation](./03-slack-transport.md) | **Done** | High | Medium |
| 04 | [Slack Message Formatting](./04-slack-formatting.md) | Deferred | Medium | Small |
| 05 | [CLI Transport Selection](./05-cli-transport-selection.md) | **Done** | High | Small-Medium |
| 06 | [Dependency and Packaging](./06-dependency-and-packaging.md) | **Done** | High | Small |
| 07 | [Slack App Setup Guide](./07-slack-app-setup-guide.md) | **Done** | Medium | Small |
| 08 | [File Upload/Download](./08-file-handling.md) | **Done** | Low | Medium |
| 09 | [Tests](./09-testing.md) | **Done** | Medium | Medium |

## Estimated Total Effort

~300-400 lines of new Python code (core transport), plus tests and docs. The clean transport abstraction in Enso means most of the work is in the Slack-specific adapter code — no core changes beyond the chat ID type fix.

## Key Reference

- **Template**: `src/enso/transports/telegram.py` — follow this structure closely
- **OpenClaw Slack adapter**: Reference for Slack API patterns (Socket Mode setup, message normalization, progressive message updates)
- **slack-bolt docs**: https://slack.dev/bolt-python/
