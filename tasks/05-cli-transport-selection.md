# Task 05: Update CLI for Transport Selection

**Status:** Done
**Priority:** High
**Depends on:** Task 03

## Description

The `enso serve` command currently hardcodes `TelegramTransport`. It needs to read the `transport` field from config and instantiate the correct transport.

## Current Code (cli.py ~line 720)

```python
transport = TelegramTransport(runtime)
runtime.transport = transport
transport.start()
```

## Target Code

```python
transport = _create_transport(config, runtime)
runtime.transport = transport
transport.start()
```

Where `_create_transport()` is a factory:

```python
def _create_transport(config: dict, runtime: Runtime) -> BaseTransport:
    name = config.get("transport", "telegram")
    if name == "telegram":
        from .transports.telegram import TelegramTransport
        return TelegramTransport(runtime)
    elif name == "slack":
        from .transports.slack import SlackTransport
        return SlackTransport(runtime)
    else:
        raise ValueError(f"Unknown transport: {name}")
```

## Also Update

- **`enso setup` command** (`_setup_transport()` function): Add Slack as a transport option in the interactive setup wizard. Ask which transport to use, then branch to `_setup_telegram()` or a new `_setup_slack()`.
- **`_setup_slack()`**: Prompt for bot token (`xoxb-...`), app-level token (`xapp-...`), and capture/set allowed user IDs.
- **`enso send` and `enso send-file` commands**: Currently hardcoded to Telegram API. Either make these transport-aware or skip for now (they're convenience features, not critical).

## Acceptance Criteria

- [ ] `enso serve` reads `config["transport"]` and starts the correct transport
- [ ] `enso setup` offers Slack as a transport option
- [ ] `enso setup` collects Slack credentials when Slack is selected
- [ ] Error message if transport is unknown
- [ ] Telegram still works exactly as before when selected
