from __future__ import annotations

import re
from dataclasses import dataclass

from .config import SUPPORTED_PROVIDERS


# `/command@botname [args]` — Telegram's group-disambiguation form.
# Normalize to `/command [args]` before parsing.
_BOT_MENTION_RE = re.compile(r"^(/\w+)@[A-Za-z0-9_]+", re.ASCII)


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

