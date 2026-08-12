# Slack rich output and persistent surfaces

This specification owns Slack response rendering, typed Block Kit output, App Home publication, and Canvas creation or replacement. Slack routing and execution authority remain owned by [teams.md](teams.md); storage remains owned by [data-model.md](data-model.md).

## Scope and defaults

Interactive Slack responses use rich output by default. `transports.slack.rich_messages` and `transports.slack.persistent_surfaces` both default to `true`; either accepts only a JSON boolean, and any malformed value fails closed as disabled. Set either key to `false` for a deliberate rollback. Persistent surfaces also require rich messages, so `rich_messages: false` disables both structured replies and surface drafts regardless of the second flag.

```jsonc
{
  "transports": {
    "slack": {
      "rich_messages": true,
      "persistent_surfaces": true
    }
  }
}
```

These features apply only to successful final answers from an interactive Slack turn. Status messages, errors, commands, `enso message send`, direct transport notifications, and scheduled-job notifications keep their existing text-only paths. Telegram and other transports receive the complete text fallback and never Slack-specific payloads.

## Interactive replies

Ordinary model output is sent through Slack's standard Markdown block, so headings, links, lists, fenced code, task lists, and Markdown tables render without conversion to Slack's older `mrkdwn` dialect. Enso splits long Markdown at Slack's 12,000-character cumulative payload limit while keeping fenced code balanced and table rows intact. An indivisible protected row or code line that cannot fit safely uses the legacy text path instead. Top-level `text` remains present as the notification and accessibility fallback.

When a layout materially helps, the runtime advertises a strict, versioned, transport-neutral structured-message contract to the model. The model may return Markdown, compact two-column fields, native tables, or a chart. Enso validates the entire response before rendering typed objects into raw Slack Block Kit dictionaries; arbitrary Slack actions, destinations, IDs, or fields are never accepted from the model.

The complete `fallback_text` is the canonical audit, accessibility, notification, and non-Slack representation. It is limited to 4,000 characters and recorded once. A recognized layout that exceeds a supported limit delivers this fallback. If Slack rejects the first block payload with `invalid_blocks`, `invalid_blocks_format`, or `msg_blocks_too_long` before any chunk was delivered, Enso retries once as text only. It never falls back after a partial multi-message delivery, and ambiguous transport or rate-limit failures are not retried because the first post may have succeeded. Malformed, unsupported, or extra-prose envelopes remain ordinary response text rather than being partially trusted.

### Supported blocks and limits

All message layouts are limited to 50 blocks.

| Layout                   | Enso contract                                                                                                                             |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Standard Markdown        | 12,000 cumulative characters per payload                                                                                                  |
| Compact fields           | 1–10 fields; 2,000 characters per field                                                                                                   |
| `data_table`             | Header plus 1–200 data rows; 1–20 columns; 20,000 cell characters; page size 1–100; raw text and finite numeric cells                     |
| `table`                  | 1–100 rows; 1–20 columns; 10,000 cell characters; optional left/center/right alignment and wrapping                                       |
| Native table aggregate   | 20,000 characters for data-table-only payloads; 10,000 across all native table cells when any simple `table` is present                   |
| Pie chart                | 1–12 positive segments; 20-character labels                                                                                               |
| Line, bar, or area chart | 1–12 unique series; 1–20 categories and matching points; 20-character series/category labels; 50-character title and optional axis labels |

Charts are message-only and limited to two per message. Native table and chart payloads are locally validated rather than sent through `blocks.validate` on every response, avoiding an extra rate-limited API call.

## Persistent surfaces

Natural-language requests can prepare a durable Slack surface when both flags are enabled. A valid model response creates only an exact, expiring draft. Enso posts a server-derived preview of the same stored payload with **Publish** and **Cancel** buttons; no Canvas or App Home API runs before the requester clicks Publish.

The confirmation is bound to the authenticated Slack account, exact route, requester, origin conversation, confirmation message, workspace, access profile, and destination. Button actions arrive through Socket Mode, are acknowledged immediately, reauthorize the current route, create any required audit evidence before mutation, and atomically consume the draft. Other users, stale cards, route changes, replays, and concurrent clicks fail closed.

Pending drafts expire after 15 minutes. Bound pending drafts survive a restart, while an interrupted in-flight publication becomes `unknown` and is never replayed automatically because Slack's mutation APIs do not provide an idempotency key. Full draft content is logically scrubbed from the draft table at every terminal transition; terminal metadata is retained for seven days. On an audited route, the separate audit records retain the exact rendered preview under the configured audit retention policy. See [data-model.md](data-model.md#slack-persistent-surface-drafts) for storage details.

### App Home

App Home is a private, complete per-user dashboard replacement. It can be proposed only from the requester's configured one-to-one DM so its exact preview is not exposed in a channel. Supported Home blocks are header, full-width plain/Markdown section, divider, compact fields, `table`, and `data_table`; message-only Markdown and chart blocks are rejected. Home views allow up to 100 blocks and 250 KB, while Enso limits confirmation-compatible drafts to 47 content blocks so the preview and controls fit in a message.

### Canvas

A channel Canvas request is resolved from the current configured channel before confirmation. If no Canvas tab exists, the card says it will create one. If exactly one existing Canvas can be identified, the card names and links it and says that its full body and title will be replaced. Enso stores the target Canvas ID and edit timestamp, re-fetches both after the click, and refuses the mutation if the target changed. Ambiguous or inaccessible channel Canvas state fails closed.

A successful replacement writes the full document first and renames it in a second API call because Slack permits one Canvas edit operation per request. A rename failure after a successful body replacement is reported as partial; ambiguous API outcomes are reported as unknown and are not retried. Enso cannot prevent an external human edit between Slack's metadata check and its unconditional edit because Slack exposes no conditional Canvas revision API.

Standalone Canvas creation is available only on paid Slack plans. Enso posts its authenticated permalink before granting fixed read-only access to the originating user or channel; multi-person DMs are unsupported. Channel placement works on free plans, which allow one Canvas tab per channel. Canvas content uses Slack Canvas Markdown. Enso enforces a conservative 1 MiB UTF-8 byte limit, 300 cells per Markdown table, and a 12,000-character limit for its exact confirmation preview.

## Slack app requirements and upgrades

The bundled [`slack_manifest.yaml`](../../src/enso/slack_manifest.yaml) enables the Home tab, Socket Mode interactivity, and the scopes required by these flows. Rich message posting uses `chat:write`; Canvas publication and link lookup use `canvases:write` and `files:read`. App Home publication itself adds no bot scope. Block actions travel over the existing Socket Mode connection, so no public interactivity URL or `block_actions` event subscription is needed. The app-level token still needs `connections:write`.

New apps created from the current manifest are ready immediately. Existing installations upgrading from an older manifest must update the Slack app from the current manifest, enable the Home tab and interactivity settings, reinstall or reauthorize the app when Slack requests consent for the added Canvas scope, and restart Enso. Running `enso setup` refreshes `~/.enso/slack-app-manifest.yaml` even when Slack credentials are left unchanged.

For a temporary code-only rollout before the Slack app is updated, set `persistent_surfaces` to `false`; ordinary rich Markdown and structured message blocks remain available. Set `rich_messages` to `false` to restore the complete legacy text path.

## Manual smoke test

After applying the manifest and restarting Enso:

1. Ask for a response containing a heading, link, fenced code, task list, and Markdown table.
1. Ask for a sortable dataset, a compact KPI summary, and each chart type; verify the visible output and text fallback describe the same data.
1. In a configured one-to-one DM, ask Enso to build an App Home dashboard. Verify that no Home change occurs before Publish, then confirm it replaces the Home view.
1. In a disposable configured channel, ask Enso to create a channel Canvas. Confirm the exact preview, publish it, then ask for an update and verify the second card clearly authorizes a full replacement of that same Canvas.
1. Click a used or expired button and verify no second mutation occurs.
