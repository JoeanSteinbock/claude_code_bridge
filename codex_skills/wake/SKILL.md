---
name: wake
description: Schedule a future turn for yourself or another agent. Use when the user says "check X in N minutes", "remind me later", "poll every N minutes", or you want to self-follow-up after a delay.
metadata:
  short-description: Schedule a future agent turn
---

# Wake (self- or cross-agent scheduled turn)

`wake` stores a scheduled invocation in `.ccb/wake_queue.json`. When the delay elapses, telegramd dispatches the stored message to the target agent as if the user had just sent it — the reply flows back through the original channel.

## When to use

- Follow-ups with a delay: *"check X in 10 min"*, *"remind me in 1h"*, *"poll every 5m"*.
- You finished a step with async downstream state (deploy, settlement, tests) and want to re-evaluate later without blocking.
- Bounded autonomous loop: finish this turn, wake yourself in N minutes with the next step.

## When NOT to use

- Immediate work → do it now.
- Synchronous delegation → use `ask <provider>` instead.
- Unbounded tight polling → schedule one wake, let the next turn decide whether to schedule another.

## Execution

```
wake <agent> --in <duration> "<message>"
```

- `<agent>`: `claude`, `codex`, `gemini`, `opencode`, `droid`, etc.
- `<duration>`: `30s`, `5m`, `1h`, `1h30m`.
- `<message>`: self-contained instruction the woken agent will see.

`wake` defaults to `caller=telegram` and auto-resolves `chat_id` from `--chat-id <ID>`, `CCB_TELEGRAM_CHAT_ID` env, or the first entry in the project's `ccb.config` `telegram.allowed_chat_ids`. For single-chat projects no explicit chat_id is needed.

## Return

Prints a `wake_id` on success and exits 0. This is async — end your turn immediately after the call (do not wait).

## Examples

- `wake claude --in 10m "check positions on the polymarket dashboard and report pnl"`
- `wake codex --in 5m "deploy check: curl web.luc.wtf/status and summarize"`
- `wake claude --in 30m "re-check trade queue; alert if >5 stuck"`

## Auxiliary

- `wake list` — pending wakes for this project.
- `wake cancel <wake_id>` — remove a pending wake.

## Rules

- End your turn immediately after `wake` exits 0.
- Prefer one deferred wake over many short chained polls.
- On non-zero exit, report the error in one line and end your turn.
