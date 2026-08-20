# Slack

Enso connects to Slack through Socket Mode, authorizes exact DM and channel routes, and
keeps replies in their originating conversation. This guide covers operator workflows;
the authoritative contracts are [teams and routes](specs/teams.md),
[response triggers](specs/slack-triggers.md), and [Slack output](specs/slack-output.md).

## Create or update the Slack app

Fresh Slack setup copies the bundled manifest to `~/.enso/slack-app-manifest.yaml` and
walks through Slack's one-paste flow:

1. Open [Create a Slack app](https://api.slack.com/apps?new_app=1).
2. Choose **From an app manifest** and select the target workspace.
3. Paste `~/.enso/slack-app-manifest.yaml` (or
   [`src/enso/slack_manifest.yaml`](../src/enso/slack_manifest.yaml)).
4. Install the app to obtain its `xoxb-` bot token.
5. Under **Basic Information → App-Level Tokens**, create an `xapp-` token with
   `connections:write`.
6. Run fresh/incomplete `enso setup` and enter both tokens.

A completed or pre-feature `enso setup` is structural-only; it does not refresh the
manifest or reconfigure Slack. For an existing app, compare and deliberately apply the
current bundled manifest while preserving local changes. Enable its Home tab and
interactivity, grant missing `canvases:write` and `files:read` scopes, reinstall or
reauthorize if Slack asks for consent, then restart Enso.

Block actions travel over Socket Mode, so no public interactivity URL or
`block_actions` event subscription is required. `chat:write` covers replies and
confirmation-card edits; App Home needs no additional bot scope. If directory event
subscriptions are removed, lookup still works but refreshes lazily.

## Exact routes and authorization

Slack credentials, options, DM routes, and channel routes live together under
`transports.slack` in `~/.enso/config.json`. There is no wildcard, default route, or
Slack `allowed_users` mode:

- A DM route is keyed by one exact Slack user ID and authorizes that person.
- A channel route is keyed by one exact channel ID and authorizes every human member who
  can post there, including administrators.
- Threads inherit the channel's workspace, policy, route settings, and response
  triggers, but keep their own provider session.
- An unlisted DM receives a fixed local access response. An explicit mention in an
  unlisted channel receives a fixed thread response. Neither invokes a provider.
- Ordinary unmentioned traffic in an unlisted channel is ignored.

Every route names one workspace, and that workspace names one policy. Use separate
workspaces when channels need different context or authority; multiple workspaces may
reuse a policy. Keep an unrestricted administrative workspace on a trusted owner DM,
not a shared channel. The [configuration guide](configuration.md) shows the complete
`route → workspace → policy → provider` model and validation commands.

## Response triggers

Authorized DMs dispatch every ordinary human message and never require a mention. A
channel route has two boolean controls, each defaulting to `true`:

| Setting | Meaning |
| --- | --- |
| `mention_required` | A top-level channel message must mention Enso |
| `thread_mention_required` | A reply in a joined thread must mention Enso |

Set defaults for already-routed channels under `transports.slack.channel_defaults` and
override them on an individual channel route. Defaults inherit settings only; they never
authorize an unlisted channel.

With `thread_mention_required: false`, Enso follows replies only after it has joined the
thread through an authorized dispatch, or when Enso posted the thread root itself. First
contact in a thread someone else started still needs a mention. Joined-thread activity
survives restarts and expires with session retention; a reply under Enso's own root is
not time-limited. Only human posts dispatch, so Enso and other apps cannot trigger a bot
loop. Channel replies always land in the triggering message's thread.

Slack `!` commands pass through these same admission rules. A responsive top level or
joined thread accepts them without a mention; a mention-gated or unjoined thread does
not. A bare `!` remains ordinary prompt text. `!use`, `!model`, and `!effort` update the
whole DM or channel even when issued inside a thread; session commands act only on the
current conversation. Slack has no `!queue`, but `!stop` clears that conversation's
in-memory queue.

For every combination and the exact event decision order, see
[Slack response triggers](specs/slack-triggers.md).

## Context and mentions

Thread messages are pushed as conversation context. When a provider session has not yet
opened, the first turn gets the full thread, including an Enso-posted root; later turns
receive only the messages since Enso last spoke. For a new top-level conversation, an
unrestricted policy receives channel-reading commands and pulls history on demand. A
restricted policy, which may have no network route to Slack, receives the recent channel
context it cannot fetch itself.

Inbound Slack user mentions are flattened to inert readable text before reaching the
provider. The leading mention that addresses Enso is removed; other mentions retain a
display name and authoritative user ID without live `<@U…>` syntax. Slack entity escaping
is decoded first. Fetched context is explicitly marked as untrusted data, never as agent
instructions.

## Slack directory and reading commands

Agents use the local account-bound Slack directory cache to turn display names into
Slack IDs and to read relevant conversation history. It refreshes on Socket Mode events
and on demand; an automatic miss refresh is limited to once per 60 seconds.

```bash
enso slack lookup-user "alex"               # name/email/display name → user
enso slack lookup-channel "general"          # name → channel
enso slack whois U0123456789                 # user ID → user
enso slack open-dm alex                      # resolve/open a DM
enso slack list [users|channels]
enso slack refresh [--users|--channels]

enso slack search "deploy failed"            # public-channel search
enso slack history C0123456789               # top-level channel messages
enso slack history C0123456789 --since 24h
enso slack history C0123456789 --all         # include lifecycle noise
enso slack thread C0123456789 <parent-ts>
enso slack thread C0123456789 <parent-ts> -n 20  # root + 19 recent replies
```

`history` excludes thread replies because Slack excludes them from channel history; use
`thread` with the root's raw `ts`. Both commands show display names, inert mentions,
forwarded bodies, readable times, and the raw timestamp. A trimmed thread reports the
number of omitted replies so it cannot be mistaken for the whole thread. During an
interactive turn, the current channel ID is available as `ENSO_ORIGIN_CHANNEL`.

## Replies and persistent surfaces

Interactive final answers use Slack's standard Markdown by default, including headings,
links, fenced code, task lists, and tables. Agents can also choose validated native
fields, tables, data tables, and line, bar, area, or pie charts when they improve the
answer. Enso safely falls back to complete text if a structured response is malformed
or Slack rejects known block validation; one malformed structured provider response may
be retried once.

Rich messages and persistent surfaces default to enabled. Set
`transports.slack.persistent_surfaces` to JSON `false` to disable App Home and Canvas
drafts while retaining rich replies. Set `rich_messages` to `false` to restore legacy
text delivery and implicitly disable persistent surfaces. Non-boolean values fail closed
as disabled. Restart Enso after either change.

A natural-language App Home or Canvas request produces an exact preview with
requester-bound **Publish** and **Cancel** buttons. Enso does not mutate Slack before
confirmation. App Home publication is accepted only in a configured one-to-one DM. A
channel Canvas request creates a tab when none exists or proposes a complete replacement
when there is one unambiguous existing Canvas. See [Slack output](specs/slack-output.md)
for limits, expiration, concurrency, security checks, and fallbacks.

Direct notifications, CLI sends, attachment captions, scheduled-job alerts, and other
non-interactive posts remain text-only. The general `enso message` workflow is in
[Operations](operations.md#send-a-message-or-file).
