# Task 01: Generalize Chat ID Types from int to str

**Status:** Done
**Priority:** High (blocking — must be done first)
**Estimated effort:** Small

## Problem

Enso's runtime uses `int` for all chat IDs (Telegram's format). Slack uses string channel IDs like `"C06ABCDEF"`. This is baked into type hints and dict keys throughout `core.py`.

## Affected Code

- `src/enso/core.py` — All per-chat state dicts:
  - `active_provider_by_chat: dict[int, str]`
  - `active_model_by_chat_provider: dict[tuple[int, str], str]`
  - `session_by_chat_provider: dict[tuple[int, str], str]`
  - `running_process_by_chat: dict[int, Process]`
  - `running_task_by_chat: dict[int, asyncio.Task]`
  - `chat_lock_by_chat: dict[int, asyncio.Lock]`
- `src/enso/core.py` — `get_active_provider()`, `get_active_model()`, `get_chat_lock()`, `stop_chat()` — all take `chat_id: int`
- `src/enso/core.py` — `load_state()` casts keys with `int(k)` (line 216)
- `src/enso/transports/telegram.py` — passes `update.effective_chat.id` (already int, no change needed there)

## Solution

Change the type alias from `int` to `int | str` (or just use a `ChatId = int | str` type alias). The runtime doesn't care about the type — it only uses chat IDs as dict keys and log labels. The state serialization already converts to `str(k)` for JSON; `load_state` just needs to not force `int()` if the value isn't numeric.

## Acceptance Criteria

- [ ] All chat ID parameters accept both `int` and `str`
- [ ] State persistence round-trips string chat IDs correctly
- [ ] Telegram transport still works unchanged (still passes int IDs)
- [ ] No runtime errors when Slack passes string channel IDs
