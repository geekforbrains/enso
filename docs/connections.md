# Connecting a chat account

`enso setup` guides a fresh local home through provider choices and private chat pairing.
Hosted setup can use the same receivers through `enso connect`. Neither flow logs into a
provider account or starts a provider during pairing. Provider subscription login remains
manual; see [Install](install.md).

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
can pair the owner. Old, forwarded, group, and unrelated messages are ignored. Setup records
the owner in `allowed_users`, binds their private chat, and uses it for notifications.
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
Its input is one JSON object, at most 16,384 characters, containing `request_id`, `bot_token`,
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
