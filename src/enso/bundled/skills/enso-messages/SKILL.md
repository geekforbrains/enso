---
name: enso-messages
description: Send messages or attachments through Enso to Slack or Telegram, notify from jobs, or inspect background delivery history.
---

# Messages

Chat replies reach the conversation automatically. Job and beat output stays in history;
use an explicit send for an authorized notification.

```bash
enso message send --file message.txt
enso message send -                       # read text from stdin
enso message attach report.pdf "Report"
enso message send --to telegram:123 --workspace team "Ready"
enso message list --json                   # current workspace; --all-workspaces available
```

Choose one text source: argument, `--file`, or stdin. Files and stdin avoid shell quoting.
Use `--json` for receipts; inspect success before chaining actions or reporting delivery.

Destination order is explicit `--to`, saved beat destination/thread, current chat origin,
then configured notify target (Slack before Telegram). Targets are `slack:C…`,
`telegram:<id>`, or a bare ID with only one transport. Explicit `--to` clears the implicit
Slack thread; use `enso-slack` for a chosen thread, identity lookup, or rich messages.
Never infer a recipient ID from a display name.

Every send needs `ENSO_WORKSPACE` or `--workspace NAME`. This owns the outbox record;
`--to` selects the destination. Sending for another workspace to another chat needs both.
The next turn reads unread background messages only for its conversation and workspace.

Inside a heartbeat run, native sends/attachments require a stable purpose-based
`--action-key`; outside one, omit it. Commands record the action and receipt themselves;
do not also reserve it with `enso heartbeat action`. A refused repeated or uncertain send
needs history inspection and reconciliation, not a new key. Load `enso-heartbeat` for that
workflow. Gates cannot send messages.
