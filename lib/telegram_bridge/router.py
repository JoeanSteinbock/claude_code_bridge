from __future__ import annotations

import re
from dataclasses import dataclass

from .config import SUPPORTED_PROVIDERS


# `/command@botname [args]` — Telegram's group-disambiguation form.
# Normalize to `/command [args]` before parsing.
_BOT_MENTION_RE = re.compile(r"^(/\w+)@[A-Za-z0-9_]+", re.ASCII)

# Duration string for /wake first-arg detection. Same grammar as the
# wake CLI's own parser: "30s", "5m", "1h", or combos like "1h30m".
_DUR_RE = re.compile(r"(?:\d+[smh])+", re.IGNORECASE)


def _strip_bot_mention(raw: str) -> str:
    return _BOT_MENTION_RE.sub(r"\1", raw, count=1)


@dataclass
class ParsedTelegramText:
    provider: str | None
    message: str
    broadcast: bool = False
    command: str = ""
    # If populated (len >= 2), the same `message` goes to each listed
    # provider. Used for mid-sentence @mentions like
    # "hey @claude, do X, and @codex, do Y".
    targets: list[str] | None = None


_MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z][a-zA-Z0-9_]*)\b")


def _collect_mentions(text: str) -> list[str]:
    """Return the ordered, de-duplicated list of `@provider` mentions
    that match a known provider name (case-insensitive). Skips mentions
    inside triple-backtick fences to avoid matching code snippets."""
    if not text:
        return []
    # Strip fenced code blocks so a ``` block containing @user doesn't
    # get misinterpreted as a routing mention.
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    seen: list[str] = []
    for m in _MENTION_RE.finditer(stripped):
        name = m.group(1).lower()
        if name in SUPPORTED_PROVIDERS and name not in seen:
            seen.append(name)
    return seen


def parse_message(text: str, default_provider: str) -> ParsedTelegramText:
    raw = (text or "").strip()
    if not raw:
        return ParsedTelegramText(provider=None, message="")
    # Normalize `/foo@botname args` → `/foo args` so group-chat mentions
    # don't bypass command detection.
    if raw.startswith("/"):
        raw = _strip_bot_mention(raw)

    if raw in ("/help", "/start"):
        return ParsedTelegramText(provider=None, message="", command="help")
    if raw == "/providers":
        return ParsedTelegramText(provider=None, message="", command="providers")

    lowered = raw.lower()

    # /new <provider>, /reset <provider> — reset a provider's pane session
    # (no AI in the loop; handled directly by telegramd).
    # Bare form (e.g. just "/new" with no target) → usage hint.
    for stem in ("/new", "/reset", "/restart", "/clear"):
        if lowered == stem:
            return ParsedTelegramText(provider=None, message="", command="new_usage")
        if lowered.startswith(stem + " "):
            rest = raw[len(stem) + 1:].strip().lower()
            target = _normalize_provider(rest) if rest else ""
            if target == "all":
                return ParsedTelegramText(provider=None, message="", command="new_all")
            if target:
                return ParsedTelegramText(provider=target, message="", command="new")
            return ParsedTelegramText(provider=None, message=rest, command="new")

    # /respawn <provider>, /relaunch <provider> — kill + relaunch the CLI.
    for stem in ("/respawn", "/relaunch"):
        if lowered == stem:
            return ParsedTelegramText(provider=None, message="", command="respawn_usage")
        if lowered.startswith(stem + " "):
            rest = raw[len(stem) + 1:].strip().lower()
            target = _normalize_provider(rest) if rest else ""
            if target == "all":
                return ParsedTelegramText(provider=None, message="", command="respawn_all")
            if target:
                return ParsedTelegramText(provider=target, message="", command="respawn")
            return ParsedTelegramText(provider=None, message=rest, command="respawn")

    # Provider-native slash commands — typed directly into the provider
    # pane (bypassing the ask/CCB_DONE protocol) because these commands
    # don't follow our reply convention. `/stats` is an alias for
    # `/status`. Default target is claude; `/compact gemini` etc. can
    # retarget.
    #
    # stem → canonical command name sent to the pane
    PASSTHROUGHS = {
        "/context": "context",
        "/compact": "compact",
        "/status": "status",
        "/stats": "status",
        "/usage": "usage",
        "/cost": "cost",
        "/config": "config",
        "/model": "model",
        "/mcp": "mcp",
        "/sessions": "sessions",
    }
    for stem, cname in PASSTHROUGHS.items():
        if lowered == stem:
            return ParsedTelegramText(provider="claude", message="", command=cname)
        if lowered.startswith(stem + " "):
            rest = raw[len(stem) + 1:].strip().lower()
            target = _normalize_provider(rest) if rest else ""
            if target and target != "all":
                return ParsedTelegramText(provider=target, message="", command=cname)
            return ParsedTelegramText(provider="claude", message="", command=cname)

    # /tail <provider> (alias /peek, /last) — snapshot the provider's
    # pane without touching it. Read-only; doesn't inject a slash
    # command or interrupt whatever the model is doing.
    for stem in ("/tail", "/peek", "/last"):
        if lowered == stem:
            return ParsedTelegramText(provider="claude", message="", command="tail")
        if lowered.startswith(stem + " "):
            rest = raw[len(stem) + 1:].strip().lower()
            target = _normalize_provider(rest) if rest else ""
            if target and target != "all":
                return ParsedTelegramText(provider=target, message="", command="tail")
            return ParsedTelegramText(provider="claude", message="", command="tail")

    # /work <agent> <duration> [hint] — wake shortcut with a work-first
    # imperative. Expands to a full /wake_add request whose message tells
    # the agent to (1) do real edits/commits, (2) report, (3) optionally
    # self-reschedule. Needed because bare "/wake … progress report or
    # schedule another" lets the model pick the lazy branch.
    if lowered == "/work" or lowered.startswith("/work "):
        body = raw[5:].strip()
        if not body:
            return ParsedTelegramText(provider=None, message="", command="work_usage")
        parts = body.split(None, 2)
        head = parts[0].lower()
        # Duration-first form: /work <duration> [hint]
        if _DUR_RE.fullmatch(head):
            agent = "claude"
            duration = head
            hint = body[len(head):].strip()
        else:
            # Agent-first form: /work <agent> <duration> [hint]
            agent = _normalize_provider(head)
            if not agent or agent == "all" or len(parts) < 2 or not _DUR_RE.fullmatch(parts[1].lower()):
                return ParsedTelegramText(provider=None, message="", command="work_usage")
            duration = parts[1].lower()
            hint = parts[2].strip() if len(parts) > 2 else ""

        task = f" on {hint}" if hint else ""
        prompt = (
            f"Work{task} for this turn — make real edits / tool calls / commits (minimum 3 substantive actions). "
            f"THEN post a concise progress report. "
            f"THEN, if the task is not done, schedule another /work {agent} {duration}"
            f"{' ' + hint if hint else ''} by calling "
            f"`wake add {agent} --in {duration} --caller telegram --chat-id <this chat> "
            f"\"{{same work prompt}}\"`. "
            f"Do NOT reply with a report-only message — real work must happen this turn."
        )
        return ParsedTelegramText(
            provider=agent,
            message=f"{duration} {prompt}",
            command="wake_add",
        )

    # /wake — schedule a future ask.
    #
    #   /wake list                                 — list pending wakes
    #   /wake cancel <id>                          — cancel a wake
    #   /wake <duration> <message>                 — default agent=claude
    #   /wake <agent> <duration> <message>         — explicit agent
    #
    # Duration is a wake-duration string (e.g. 30s, 5m, 1h30m). The
    # message keeps its original casing; only the routing prefix is
    # lowercased.
    if lowered == "/wake" or lowered.startswith("/wake "):
        body = raw[5:].strip()
        if not body:
            return ParsedTelegramText(provider=None, message="", command="wake_usage")
        parts = body.split(None, 1)
        head = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if head == "list":
            return ParsedTelegramText(provider=None, message="", command="wake_list")
        if head == "cancel":
            return ParsedTelegramText(provider=None, message=rest.strip(),
                                      command="wake_cancel")
        # Duration as the first token → default agent=claude.
        if _DUR_RE.fullmatch(head):
            return ParsedTelegramText(provider="claude", message=f"{head} {rest}".strip(),
                                      command="wake_add")
        # Otherwise first token must be an agent name.
        target = _normalize_provider(head)
        if target and target != "all" and rest:
            return ParsedTelegramText(provider=target, message=rest.strip(),
                                      command="wake_add")
        return ParsedTelegramText(provider=None, message="", command="wake_usage")

    if lowered.startswith("/ask "):
        parts = raw.split(None, 2)
        if len(parts) >= 3:
            provider = _normalize_provider(parts[1])
            return ParsedTelegramText(provider=provider, message=parts[2].strip(), broadcast=(provider == "all"))

    for sep in (":", ",", " ", "\n"):
        for prefix in ["all", *SUPPORTED_PROVIDERS]:
            head = prefix
            if lowered.startswith(f"{head}{sep}"):
                body = raw[len(head) + len(sep):].strip()
                return ParsedTelegramText(
                    provider=None if prefix == "all" else prefix,
                    message=body,
                    broadcast=(prefix == "all"),
                )
            mention = f"@{head}"
            if lowered.startswith(f"{mention}{sep}"):
                body = raw[len(mention) + len(sep):].strip()
                return ParsedTelegramText(
                    provider=None if prefix == "all" else prefix,
                    message=body,
                    broadcast=(prefix == "all"),
                )
            slash = f"/{head}"
            if lowered.startswith(f"{slash}{sep}"):
                body = raw[len(slash) + len(sep):].strip()
                return ParsedTelegramText(
                    provider=None if prefix == "all" else prefix,
                    message=body,
                    broadcast=(prefix == "all"),
                )

    # `@all` anywhere in the text → broadcast to configured providers.
    # Using a word-boundary regex so we don't match inside emails or
    # usernames like `@allen_123`.
    if re.search(r"(?<![\w@])@all\b", raw, re.IGNORECASE):
        # Strip `@all` (and an adjacent comma/space) from the body so
        # the providers don't see the routing token.
        body = re.sub(r"(?<![\w@])@all\b[,:]?\s*", "", raw, flags=re.IGNORECASE).strip()
        return ParsedTelegramText(provider=None, message=body, broadcast=True)

    # Mid-sentence @mentions: if two or more provider mentions appear
    # anywhere in the text, route the whole message to each of them.
    # Single mention falls through to the existing per-provider prefix
    # logic above (so this only kicks in for multi-target intent).
    mentions = _collect_mentions(raw)
    if len(mentions) >= 2:
        return ParsedTelegramText(
            provider=None,
            message=raw,
            targets=mentions,
        )

    return ParsedTelegramText(provider=None, message=raw)


def _normalize_provider(value: str) -> str:
    provider = (value or "").strip().lower()
    if provider == "all":
        return "all"
    if provider in SUPPORTED_PROVIDERS:
        return provider
    return ""


def help_text(default_provider: str, broadcast_providers: list[str]) -> str:
    providers = ", ".join(SUPPORTED_PROVIDERS)
    all_targets = ", ".join(broadcast_providers) if broadcast_providers else "(none)"
    return (
        "CCB Telegram bridge\n\n"
        "Formats:\n"
        "  claude: your question\n"
        "  /codex explain this code\n"
        "  /ask gemini summarize this\n"
        "  all: compare this approach\n\n"
        f"Default provider: {default_provider}\n"
        f"Broadcast targets for 'all': {all_targets}\n"
        f"Providers: {providers}\n"
        "Commands:\n"
        "  /help\n"
        "  /providers\n"
        "  /new <provider> — reset that provider's session (or `all`)\n"
        "  /reset <provider> — alias for /new"
    )

