---
name: wake
description: Schedule a future turn for yourself or another agent. Use when the user says "check X in N minutes", "remind me", "poll every N minutes", "follow up later", or when you finished a step and want to re-evaluate after some delay without blocking.
metadata:
  short-description: Schedule a future agent turn
---

# Wake (self- or cross-agent scheduled turn)

`wake` stores a scheduled invocation in the project's `.ccb/wake_queue.json`. When the delay elapses, telegramd dispatches the stored message to the target agent as if the user had just sent it — the reply then flows back to the original channel.

## When to use

- User asks for a follow-up after some delay: *"check the deploy in 10 min"*, *"remind me to review positions in 1h"*, *"poll the build every 5m"*.
- You completed a task that has asynchronous downstream state (a running deploy, a pending settlement, a test suite) and want to self-check later without blocking this turn.
- You want a bounded autonomous loop: finish this turn, wake yourself in N minutes with the next step.

## When NOT to use

- Immediate work the user wants **now** → just do it.
- Delegating to another provider synchronously → use `ask <provider>` instead.
- Long-running polling loops with no bound → prefer a single follow-up wake; don't chain dozens of short wakes.

## Execution (MANDATORY)

```
Bash(wake <agent> --in <duration> "<message>")
```

- `<agent>`: `claude`, `codex`, `gemini`, `opencode`, `droid`, etc.
- `<duration>`: `30s`, `5m`, `1h`, or combos like `1h30m`.
- `<message>`: the prompt delivered to the target agent when the wake fires. Write it as a self-contained instruction — the target sees only this message.

Caller context: `wake` defaults to `caller=telegram` and auto-resolves `chat_id` from (in order) `--chat-id <ID>` flag, `CCB_TELEGRAM_CHAT_ID` env, then the first entry in the project's `ccb.config` `telegram.allowed_chat_ids`. For single-chat projects (the common case), no explicit chat_id is needed.

## Return

On success, `wake` prints a `wake_id` (e.g. `20260418-153256-843-27361-1`) and exits 0. This is async — **end your turn immediately** after calling it, similar to the `ask` async guardrail. Do not wait.

## Examples

- User: *"Check positions in 10 min"*
  → `wake claude --in 10m "check positions on the polymarket dashboard and report the pnl"`
  → Reply: `Scheduled a check in 10m (wake_id: <id>).`

- User: *"ping me in 5 min to confirm deploy"*
  → `wake claude --in 5m "deploy check: query /status on web.luc.wtf and report"`

- Self-follow-up: after finishing a step, delay a re-evaluation by 30 min.
  → `wake claude --in 30m "re-check the trade queue; if >5 stuck, alert me"`

## Auxiliary commands

- `wake list` — show pending wakes for this project.
- `wake cancel <wake_id>` — remove a pending wake.

## Rules

- Always end your turn immediately after `wake` exits 0 (the scheduled turn will arrive later on its own).
- Do not chain wakes in tight loops. If you need repeated polling, schedule one wake and let the next turn decide whether to schedule another.
- If `wake` fails (non-zero exit), report the error in one line and end your turn.
