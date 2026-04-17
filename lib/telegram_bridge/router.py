from __future__ import annotations

from dataclasses import dataclass

from .config import SUPPORTED_PROVIDERS


@dataclass
class ParsedTelegramText:
    provider: str | None
    message: str
    broadcast: bool = False
    command: str = ""


def parse_message(text: str, default_provider: str) -> ParsedTelegramText:
    raw = (text or "").strip()
    if not raw:
        return ParsedTelegramText(provider=None, message="")

    if raw in ("/help", "/start"):
        return ParsedTelegramText(provider=None, message="", command="help")
    if raw == "/providers":
        return ParsedTelegramText(provider=None, message="", command="providers")

    lowered = raw.lower()
    if lowered.startswith("/ask "):
        parts = raw.split(None, 2)
        if len(parts) >= 3:
            provider = _normalize_provider(parts[1])
            return ParsedTelegramText(provider=provider, message=parts[2].strip(), broadcast=(provider == "all"))

    for sep in (":", " ", "\n"):
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
        "  /providers"
    )

