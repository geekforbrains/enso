# Connecting a chat account

`enso setup` guides a fresh local home through provider choices and private chat pairing.
Hosted setup can use the same receivers through `enso connect`. Neither flow logs into a
provider account or starts a provider during pairing. Provider subscription login remains
manual; see [Install](install.md).

## Access in 0.2.0

**Implemented on the 0.2.0 development branch.**
A binding both grants access to the installation and selects an existing Enso workspace.
The key uses platform-issued channel or user IDs from authenticated
transport events. Display names and identities claimed in message text never grant access.
Transport authentication and connection identity checks still apply.
Admission is decided before attachment download, provider work, or capture.

| Conversation | Required binding | Who can use Enso there |
| --- | --- | --- |
| Slack channel | Channel ID; the bot must also be present | Its human participants |
| Slack one-to-one DM | Sender's user ID | That explicitly bound person |
| Telegram private chat | Sender's user ID | That explicitly bound person |

Binding a channel trusts its audience to use the installation's capabilities and
[captures eligible live human messages](memory.md#conversation-capture) there. That audience
includes later additions, guests, and external Slack Connect participants. Channel membership does not grant DM access.
`mention_required` and `thread_mention_required` control replies; they do not change access
or [capture eligibility](memory.md#conversation-capture).

There is no wildcard DM access, automatic personal workspace, fallback for an unknown
sender, or separate user/role permission system. Telegram stays private-chat-only;
unsupported group conversations and bot-originated messages never enter agent dispatch.
Telegram's `allowed_users` setting is removed: explicit bindings are the sole access list.
See [Configuration](configuration.md#bindings) for key syntax and validation.

An unbound Slack channel receives one short canned notice when the bot is mentioned; an
unbound Slack DM or Telegram private chat receives it on any human message. Otherwise an
unbound channel stays silent. The fixed notice is "This conversation is not bound to an
available workspace." It contains no private configuration details and requires no provider
call, attachment download, or capture. A binding naming a missing workspace logs a diagnostic
and sends the same notice, without selecting another workspace. This admission check also
applies to chat commands and repeats before deferred attachment preparation.

Bindings are read for incoming messages. A queued turn retains the workspace selected on
arrival; removing its binding withdraws access before it starts. Its original workspace
and the currently bound workspace must both still exist. Rebinding does not move
past records or resume a session in a different workspace. Removing a binding preserves
past captures and maintained memory.

Only trusted configuration or operator-initiated pairing creates a binding. Preserve the
short-lived challenge and acknowledgment described below. Pairing messages never become
agent turns or memory captures; an unknown sender cannot authorize their own binding by
asking Enso. Telegram pairing writes the explicit user binding and notification target.

Several bindings can select one workspace; each named workspace must already exist:

| Example | Result |
| --- | --- |
| One person's `telegram:123456` and `slack:dm:U0123` both bind to `default` | One personal workspace, separate transport conversations |
| `slack:C0123` binds to `product`; `slack:C0456` binds to `support` | Two team channels with their own workspace context in one installation |
| Two people's DM bindings both select `team` | Shared maintained memory, separate conversations and provider sessions |
| Those DM bindings select `alex` and `sam` respectively | Separate personal context and ownership within the same trusted installation |

Personal workspaces do not promise confidentiality from other agents in the installation.
Teams needing separation use the separate-machine arrangement in the
[installation trust model](concepts.md#installation-trust-model).

## Pairing

Slack needs the Bot User OAuth Token and an App-Level Token with `connections:write`.
Export the bundled app definition with `enso slack manifest`. Setup checks bot identity,
opens Socket Mode with the app token, and checks that both tokens belong to the same app.
It then gives the user a private-chat link and a fresh `ENSO-…` code. Only a new direct
message containing that exact code from a human account can pair the owner. Thread replies,
bot messages, and events from another workspace do not pair an account. The resulting
`slack:dm:USER` binding and notification target use that owner's private conversation.

Telegram needs the bot token created by [BotFather](https://t.me/BotFather) with `/newbot`.
Setup verifies bot identity and refuses an existing webhook. A fresh `t.me` Start link
carries a random challenge. Only the matching new private `/start` from a human account
can pair the owner. Old, forwarded, group, and unrelated messages are ignored.
Setup binds the owner's private chat and uses it for notifications.
A Telegram `409` stops pairing and asks the user to stop the competing receiver. Setup never
deletes a webhook or takes over another polling process automatically.

Each receiver waits for at most five minutes, then closes. Success sends one connection
acknowledgment before closing; it does not dispatch the pairing message to an AI provider.
An acknowledgment failure is a pairing failure. `serve`, native setup, and hosted pairing
share a per-home receive lock, so only one of them can receive messages at a time. The normal
service must stop before a new connection or changed hosted defaults are applied.

## Hosted command contract

```text
enso connect start --transport slack|telegram --file FILE|- [--json]
enso connect status [ATTEMPT_ID] [--json]
enso connect cancel [ATTEMPT_ID] [--json]
enso connect finish --file FILE|- [--json]
```

`start` initializes the home if needed; a new attempt requires an absent active config.
Its input is one JSON object, at most 16 KiB (16,384 bytes), containing `request_id`, `bot_token`,
and for Slack `app_token`. The request ID is 16–128 letters, digits, underscores, or hyphens. Supply secrets
through stdin or a private file, never command arguments. Retrying the latest request ID
with the same transport and credentials returns its existing attempt, even after apply;
changed credentials or a fresh attempt require a new ID. Only the latest attempt is retained.
A second receiver is refused until the first closes or is cancelled.

`status` and `cancel` without an ID operate on the latest attempt. Supplying a different ID
fails instead of affecting a newer attempt. The ID is a positional argument, for example
`enso connect status ATTEMPT_ID --json`, not an `--attempt-id` option.
Cancellation invalidates the challenge, closes the receiver, and removes temporary
credentials. An interrupted process is reconciled on the next status read. Cancel never
removes or changes an applied config.

`finish` accepts exactly `attempt_id`, `defaults` (the `provider`, `model`, `effort` triple),
and `expected_hash`. The agent must be a bundled model and an effort that its adapter accepts
without clamping; use [`enso providers --json`](cli.md#onboarding-contracts) for offline
choices, not as an authentication check. For the first apply use `"missing"`. It constructs
the complete config on the local machine from the paired identity and private credentials,
then uses validated, atomic [configuration apply](configuration.md#applying-configuration). A repeated successful
apply is idempotent, including a lost command response. Once applied, the same attempt can
revise its AI defaults using the current hash; other config fields are preserved. A config
created outside this pairing flow is preserved and refused. Changing transports requires a
fresh home; existing connections retain the ordinary manual configuration workflow.

All four commands with `--json` emit one result and exit 0 on success:

```json
{
  "version": 1,
  "ok": true,
  "connection": {
    "attempt_id": "…", "request_id": "…", "transport": "slack", "state": "waiting",
    "bot_name": "enso", "workspace_name": "My workspace",
    "open_url": "https://slack.com/app_redirect?…", "instruction": "ENSO-…",
    "expires_at": "2026-09-09T20:00:00+00:00"
  },
  "config_valid": false,
  "config_hash": "missing",
  "first_reply": null
}
```

`connection` is null before an attempt. Fields not yet known are omitted. States are
`verifying`, `waiting`, `paired`, `applied`, `failed`, `cancelled`, and `expired`.
`paired` is exposed only after the receiver releases its connection. Terminal failures add
`error` and `error_code`; applied state adds `defaults`. `config_valid` means local validation
passed, not that a provider can respond. `config_hash` identifies the current exact config.
A successful `status` command can still report a failed or expired connection: consumers
must inspect `connection.state`, not only the top-level `ok`.

Command failures exit 1 with `{"version":1,"ok":false,"error":"…","error_code":"…"}`.
Codes distinguish `invalid_input`, `already_configured`, `busy`, `conflict`, `missing_extra`,
`invalid_token`, `missing_scope`, `mismatched_app`, `webhook_conflict`, `polling_conflict`,
`connection_failed`, `timeout`, `cancelled`, `expired`, `not_paired`, `not_found`,
`config_invalid`, and `storage_error`. Messages are safe for display and never contain a
third-party response or submitted bot credentials. Invalid CLI syntax still exits 2.

## Private state and completion

Hosted pairing stores its pending credentials in `cache/connect/credentials.json` with
owner-only file access, inside an owner-only directory. The attempt state, advisory locks,
and optional reply receipt live alongside it. Pending credentials remain while a paired
owner finishes setup; failure, expiry, cancellation, or successful apply removes that
temporary file. The applied tokens remain literal values in the private `config.json`.
Native setup keeps credentials in memory until its config write.

First-reply receipts belong to the hosted `connect` flow; native `setup` does not create a
hosted attempt or receipt. After manual provider login and service start, the user sends a
normal message. Runtime records a receipt only after delivering a successful answer using
the selected provider, model and effort to the paired owner and conversation. Empty responses, provider errors,
failed formatting repairs, timeouts, cancellations, and failed delivery do not qualify.
The receipt contains the attempt ID, agent triple, transport, owner, conversation, timestamp,
and config hash; it contains no prompt or response text. It matches the configuration
snapshot the answering turn ran with — each turn reads `config.json` fresh — and the hash
saved by `connect finish`. A config change hides old completion evidence. Reconcile changed
configuration with `connect finish` using the current hash, then get a successful reply
from that configuration before expecting a new receipt; the next turn already runs under
the changed file, but changing the file alone does not update the pairing attempt's saved
hash.
