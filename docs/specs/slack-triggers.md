# Slack response triggers

When a Slack message engages Enso at all. This document owns per-channel mention requirements, thread following, how inbound mentions are rewritten for the model, and when `!` commands are recognized. [teams.md](teams.md) owns who may invoke a route and what it executes; [slack-output.md](slack-output.md) owns how authorized replies render.

DM behavior is fixed and outside these settings: an authorized DM always dispatches every ordinary message, never requires a mention, and never threads.

## Model

Channel routes gain two boolean response triggers:

| Setting                   | Default | Meaning                                                                                        |
| ------------------------- | ------- | ---------------------------------------------------------------------------------------------- |
| `mention_required`        | `true`  | Whether a top-level channel message must @-mention the bot to dispatch                         |
| `thread_mention_required` | `true`  | Whether a reply in a thread Enso already participates in must @-mention the bot to dispatch    |

The defaults reproduce Enso's original behavior exactly: existing configurations continue to work unchanged, mention-gated in channels, with no config migration.

The four combinations:

| `mention_required` | `thread_mention_required` | Behavior                                                                                     |
| ------------------ | ------------------------- | -------------------------------------------------------------------------------------------- |
| `true`             | `true`                    | Original behavior: every dispatch needs a mention, top-level or threaded                     |
| `true`             | `false`                   | Mention starts a conversation; Enso then follows every reply in that thread                  |
| `false`            | `true`                    | Every top-level message gets a threaded reply; thread replies still need a mention           |
| `false`            | `false`                   | Fully responsive: every top-level message and every reply in a joined thread dispatches      |

Regardless of settings, a reply to a **channel** message is always delivered in that message's thread. A non-mention top-level dispatch threads its reply exactly like a mention does today.

`thread_mention_required: false` applies only to threads Enso is already in. The first contact inside a thread someone else started still requires a mention; that dispatch joins the thread and following begins with the next reply. A top-level dispatch (mention, or any message when `mention_required: false`) joins the thread it starts, and so does a top-level message Enso posts itself — see [Thread participation](#thread-participation).

## Configuration

Both settings are valid on channel routes and in a `transports.slack.channel_defaults` block. A key on a channel route overrides `channel_defaults`; an absent key falls back to `channel_defaults`, then to the built-in `true`.

```jsonc
"transports": {
  "slack": {
    "bot_token": "xoxb-...",
    "app_token": "xapp-...",
    "account_id": "T0YOURTEAM",

    // Applies to every channel route that does not set its own value.
    // This is settings inheritance, not authorization: it routes nothing
    // by itself, and unrouted channels remain unrouted.
    "channel_defaults": {
      "mention_required": false,
      "thread_mention_required": false
    },

    "channels": {
      // Fully responsive via channel_defaults.
      "C0COMPANY": { "workspace": "company", "audit": false },

      // Client channel opts back into mention-only.
      "C0ACME": {
        "workspace": "acme",
        "audit": true,
        "mention_required": true,
        "thread_mention_required": true
      }
    }
  }
}
```

Validation is fail-closed like the rest of `transports.slack`:

- `channel_defaults` must be an object; unknown keys inside it are configuration errors.
- Both settings must be booleans wherever they appear.
- Neither key is valid on a DM route: DM behavior is not configurable, and silently accepting the key would misrepresent what the config does.
- Effective values are resolved at load time onto each route, so `enso route explain` reports what a channel will actually do.

## Event handling

For a channel message the decision order is:

1. Ignored subtypes, messages without a user, and machine-authored posts are dropped: Enso's own messages, other Slack apps' posts (`bot_id`/`bot_profile` — modern app posts carry no subtype), and Slackbot. Channel routes authorize human members, so a feed bot whose content embeds a mention token never becomes an authorized request, and two auto-responsive bots cannot reply to each other in a loop.
2. The channel is resolved against exact routes. A non-mention message in an unrouted channel is dropped silently — no reply, no ledger row. Explicit contact (a mention, or any DM) at an unrouted location keeps its fixed local response, unchanged.
3. A top-level message without a mention is dropped unless the route's effective `mention_required` is `false`.
4. A thread reply without a mention is dropped unless the route's effective `thread_mention_required` is `false` **and** Enso participates in that thread — retained conversation activity from a prior dispatch, or a thread root Enso posted itself.
5. Everything that survives claims the delivery ledger and dispatches through the normal route pipeline: same session keys, same audit rules, same policy checks.

A route whose binding fails at dispatch time (unusable native policy, no launchable provider, blocked audit) reports its fixed error reply only to explicit ordinary contact. Unaddressed ordinary traffic admitted by relaxed triggers fails silently — audited routes still record the blocked turn — so a broken responsive channel is not spammed on every message. An admitted `!` command remains a command and receives its command response without a mention.

Non-mention drops happen before the ledger claim so a busy fully-ignored channel writes nothing. Mentions are delivered by Slack both as `app_mention` and as `message` events; whether a message counts as a mention is decided by inspecting its text for the bot's user ID, not by which event delivered it, and the ledger's delivery claim keeps the duplicate pair to one dispatch.

Top-level channel dispatches under restricted policies fetch recent channel context whether
or not they were mentions. Unrestricted policies do not receive pushed top-level history;
on the conversation's first turn they instead receive `enso slack history` and
`enso slack thread` guidance so they can pull only what they need. Thread dispatches fetch
thread context under either authority mode, as before.

Thread context normally starts after Enso last spoke, because its own messages are already in the provider session. A conversation with no session memory yet — no session at all, or one reserved but never used — has no such backstop, so it receives the whole thread including Enso's own messages. This is what carries an Enso-posted thread root to the model: that root predates every session, and Enso is the last speaker before each reply, so the since-last-spoke slice would be empty and the root would never arrive. It is sent once, on the turn that opens the session; later turns in the thread go back to the narrow slice.

### Thread participation

Enso participates in a thread in either of two ways.

**A prior authorized dispatch**, which records activity for the per-thread conversation key independently of whether a provider session exists yet. The marker is persisted with session state, so following survives restarts, and it expires with session retention (`ENSO_SESSION_TTL_DAYS`, default 30 days idle) — a thread that has been quiet that long needs a fresh mention, which re-joins it.

**Enso posted the thread root itself.** A top-level message Enso posts outside a dispatch — a job notification, `enso message send`, a surface confirmation — creates no retained conversation-activity marker, so that marker alone would ignore every reply under Enso's own posts until someone mentioned the bot once. Slack stamps each thread reply with `parent_user_id`, so this is read from the event itself and costs no API call. It is not time-limited: unlike retained conversation activity, a reply under an old Enso post still engages, starting a fresh session. An event without `parent_user_id` falls back to the retained-activity check.

Only Enso's own roots count. A thread rooted by another person or another app still requires a first mention, and `thread_mention_required: true` gates own roots like any other. As everywhere else, only human replies dispatch — Enso's own messages in the thread never do, so it cannot answer itself.

## Mention rewriting

Inbound user mentions are flattened to inert text before the model sees them, in the request itself and in fetched thread/channel context:

- A leading mention of the bot is addressing, not content, and is removed.
- Any other mention of the bot becomes `@<bot display name>`, so the model knows when it is being talked about.
- A mention of anyone else becomes `@<display name> (<user ID>)`, resolved through the Slack directory cache, falling back to `@<user ID>` on a cache miss. The name is for reading; the ID stays authoritative for lookups (`enso slack`).

Raw `<@U…>` syntax never reaches the prompt. Flattening solves two problems at once: the model still sees who a request is about, and mention syntax echoed back by the model cannot render as a live ping, since outbound mrkdwn is not escaped. Flattened text pings no one. Special mentions such as `<!here>` are untouched by this pass; they carry no user identity.

Display names are user-controlled, so they are neutralized before interpolation — angle brackets, square brackets, and line breaks are removed. A crafted profile name can neither reintroduce live mention syntax through the flattener nor forge the `[user …]` author labels on injected context.

Audit turns record the flattened request text.

## Commands

Response-trigger admission happens before command parsing. In a mention-gated channel or thread, an unmentioned `!status` is dropped like any other message. When `mention_required: false`, a top-level `!status` needs no mention; when `thread_mention_required: false`, the same is true in a thread Enso already participates in. First contact in someone else's thread remains gated. Once admitted, any non-bare `!` prefix is a command: unknown and policy-disallowed names receive their normal command response, while a bare `!` remains ordinary prompt text.

Relaxing a response trigger therefore makes the existing command surface reachable without a mention; it does not authorize more commands. The route's workspace policy still applies `chat_commands` after admission, and machine-authored or unrouted traffic remains excluded earlier in the pipeline.

`!use`, `!model`, and `!effort` may be run at a channel root or inside a thread, but they always update the entire channel route; their response names that scope. In a Slack DM they apply to the entire DM. Passing `default` clears the corresponding explicit choice. Session commands remain conversation-specific: `!stop`, `!clear`, and `!compact` act on the current DM/root/thread. Slack has no `!queue` command; `!stop` still clears the current conversation's in-memory queue.
