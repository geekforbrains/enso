# Task 07: Slack App Setup Guide

**Status:** Done
**Priority:** Medium
**Depends on:** Task 03

## Description

Users need to create a Slack app before they can use the Slack transport. Document the required steps and permissions.

## Slack App Requirements

### App Creation
1. Go to https://api.slack.com/apps
2. Create New App → From Scratch
3. Name it (e.g., "Enso Agent") and select workspace

### OAuth Scopes (Bot Token Scopes)
Required scopes for the bot token (`xoxb-...`):
- `chat:write` — send messages
- `chat:write.customize` — customize bot name/icon per message (optional)
- `channels:history` — read messages in public channels
- `groups:history` — read messages in private channels
- `im:history` — read DMs
- `im:write` — open DMs for notifications
- `app_mentions:read` — receive @mention events
- `files:read` — read uploaded files (for file handling)
- `files:write` — upload files (for sending files back)
- `reactions:write` — add reactions (optional, for status indicators)

### Socket Mode
1. Enable Socket Mode in app settings
2. Generate an app-level token (`xapp-...`) with `connections:write` scope

### Event Subscriptions
Subscribe to bot events:
- `message.im` — DM messages
- `message.channels` — public channel messages (if desired)
- `message.groups` — private channel messages (if desired)
- `app_mention` — @mentions

### Install to Workspace
1. Install the app to your workspace
2. Copy the Bot User OAuth Token (`xoxb-...`)
3. Copy the App-Level Token (`xapp-...`)

## Deliverable

Add setup instructions either:
- In the README.md under a "Slack Setup" section
- As output during `enso setup` when Slack is selected
- Or both

## Acceptance Criteria

- [ ] Clear step-by-step instructions for creating the Slack app
- [ ] All required scopes are documented
- [ ] Socket Mode setup is documented
- [ ] Event subscriptions are documented
- [ ] Instructions for getting both tokens (xoxb and xapp)
