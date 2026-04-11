# Task 06: Add slack-bolt Dependency and Packaging

**Status:** Done
**Priority:** High
**Depends on:** None (can be done in parallel)

## Description

Add `slack-bolt` and `slack-sdk` as optional dependencies so users who only want Telegram don't need to install Slack packages.

## Changes

### pyproject.toml

```toml
[project.optional-dependencies]
slack = ["slack-bolt>=1.18", "slack-sdk>=3.27"]
dev = ["ruff>=0.15.0", "pytest>=8.0", "pytest-asyncio>=0.23"]
all = ["slack-bolt>=1.18", "slack-sdk>=3.27"]
```

This lets users install with:
- `pip install enso` — Telegram only (existing behavior)
- `pip install enso[slack]` — adds Slack support
- `pip install enso[all]` — everything

### Import Guard in SlackTransport

```python
try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError:
    raise ImportError(
        "Slack support requires slack-bolt. Install with: pip install enso[slack]"
    )
```

## Acceptance Criteria

- [ ] `pip install enso` works without slack-bolt installed
- [ ] `pip install enso[slack]` installs slack-bolt and slack-sdk
- [ ] Clear error message if user configures Slack transport without the package installed
