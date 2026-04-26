from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError


_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_BOLD_DOUBLE_UNDERSCORE_RE = re.compile(r"__([^_\n]+?)__")


def to_telegram_markdown(text: str) -> str:
    """
    Convert GitHub-flavored markdown (which models emit by default) into
    Telegram legacy-Markdown — `**bold**` → `*bold*`, `__bold__` → `_bold_`.

    Why: most providers are trained to emit `**bold**`. Telegram's legacy
    Markdown parser uses single asterisks; without conversion replies show
    raw `**` characters instead of formatting.
    """
    if not text:
        return text
    text = _BOLD_DOUBLE_RE.sub(r"*\1*", text)
    text = _BOLD_DOUBLE_UNDERSCORE_RE.sub(r"_\1_", text)
    return text


@dataclass
class TelegramApiError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


class TelegramBotClient:
    def __init__(self, token: str, *, http_timeout: float = 65.0):
        token = (token or "").strip()
        if not token:
            raise TelegramApiError("bot token is required")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}"
        self.http_timeout = float(http_timeout)

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def set_my_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        """Register the bot's `/` autocomplete menu (Telegram Bot API).

        `commands` is a list of `{"command": "new", "description": "..."}`
        dicts. Command names must match `[a-z0-9_]{1,32}`.
        """
        return self._call("setMyCommands", {"commands": list(commands)})

    def set_message_reaction(
        self,
        chat_id: str | int,
        message_id: int,
        emoji: str,
        *,
        is_big: bool = False,
    ) -> dict[str, Any]:
        """Attach an emoji reaction to a specific message.

        Telegram only accepts a curated set of reaction emojis for non-Premium
        bots (👍 👎 ❤️ 🔥 👏 😁 🤔 😱 🎉 🤯 😢 🙏 🤝 🤗 💯 etc.). The call will
        error out with 400 if the emoji is unsupported.
        """
        return self._call(
            "setMessageReaction",
            {
                "chat_id": str(chat_id),
                "message_id": int(message_id),
                "reaction": [{"type": "emoji", "emoji": emoji}],
                "is_big": bool(is_big),
            },
        )

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": int(timeout)}
        if offset is not None:
            payload["offset"] = int(offset)
        if allowed_updates is not None:
            # Telegram defaults to "all except a few opt-in types" when the
            # field is omitted. Pass it explicitly when we need to opt in
            # to newer types like `managed_bot`. Caller must enumerate
            # everything they still want — Telegram treats this list as a
            # hard filter, not an extension.
            payload["allowed_updates"] = list(allowed_updates)
        result = self._call("getUpdates", payload)
        if isinstance(result, list):
            return result
        return []

    def get_managed_bot_token(self, *, user_id: int) -> str:
        """Fetch the bot token for a managed bot we own. `user_id` is the
        managed bot's Telegram User ID (from `update["managed_bot"].bot.id`).
        Empirically: Telegram's API responds 400 BOT_ACCESS_FORBIDDEN if
        we don't actually own the bot, and 400 'user not found' for an
        unknown id."""
        result = self._call("getManagedBotToken", {"user_id": int(user_id)})
        return str(result) if result else ""

    def send_chat_action(self, chat_id: str | int, action: str = "typing") -> dict[str, Any]:
        # Telegram shows the "…is typing" status for ~5 seconds; callers must
        # re-send this periodically while work is in progress.
        return self._call("sendChatAction", {"chat_id": str(chat_id), "action": action})

    def get_file(self, file_id: str) -> dict[str, Any]:
        """Look up a file's metadata (including file_path) by file_id."""
        return self._call("getFile", {"file_id": str(file_id)})

    def download_file(self, file_id: str, dest_dir: Path, *, preferred_name: str = "") -> Path:
        """Download a Telegram file by id into dest_dir, return the local Path.

        Uses getFile → streamed download of the resulting file_path.
        """
        meta = self.get_file(file_id)
        if not isinstance(meta, dict):
            raise TelegramApiError(f"getFile returned unexpected payload for {file_id}")
        file_path = str(meta.get("file_path") or "").strip()
        if not file_path:
            raise TelegramApiError(f"file_path missing for file_id={file_id}")

        url = f"{self._file_base_url}/{file_path}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = (preferred_name or Path(file_path).name or file_id).strip() or file_id
        dest = dest_dir / name
        # Avoid collisions: append counter if the name already exists.
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            for i in range(1, 1000):
                candidate = dest_dir / f"{stem}-{i}{suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
        req = request.Request(url, method="GET")
        try:
            with request.urlopen(req, timeout=self.http_timeout) as resp, dest.open("wb") as out:
                shutil.copyfileobj(resp, out)
        except HTTPError as exc:
            raise TelegramApiError(f"HTTP {exc.code} downloading {file_path}") from exc
        except URLError as exc:
            raise TelegramApiError(f"Network error downloading {file_path}: {exc}") from exc
        return dest

    def send_message(self, chat_id: str | int, text: str, *, reply_to_message_id: int | None = None) -> dict[str, Any]:
        original = text or ""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": to_telegram_markdown(original),
            "disable_web_page_preview": True,
            "parse_mode": "Markdown",
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        try:
            return self._call("sendMessage", payload)
        except TelegramApiError as exc:
            msg = str(exc).lower()
            if "parse" in msg or "entity" in msg or "can't find end" in msg:
                # Unbalanced markup — retry as plain text so the message at
                # least gets through.
                payload.pop("parse_mode", None)
                payload["text"] = original
                return self._call("sendMessage", payload)
            raise

    def send_message_with_url_button(
        self,
        chat_id: str | int,
        text: str,
        *,
        button_text: str,
        button_url: str,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """sendMessage with a single URL button (inline keyboard).
        Used by /mount to surface the t.me/newbot deep link as a tap
        target instead of a bare URL."""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": to_telegram_markdown(text or ""),
            "disable_web_page_preview": True,
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [[{"text": button_text, "url": button_url}]]
            },
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        try:
            return self._call("sendMessage", payload)
        except TelegramApiError as exc:
            msg = str(exc).lower()
            if "parse" in msg or "entity" in msg or "can't find end" in msg:
                payload.pop("parse_mode", None)
                payload["text"] = text or ""
                return self._call("sendMessage", payload)
            raise

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{method}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with request.urlopen(req, timeout=self.http_timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TelegramApiError(f"HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise TelegramApiError(f"Network error: {exc}") from exc
        except Exception as exc:
            raise TelegramApiError(f"Telegram API request failed: {exc}") from exc

        try:
            parsed = json.loads(body)
        except Exception as exc:
            raise TelegramApiError(f"Invalid Telegram API response: {exc}") from exc
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            description = ""
            if isinstance(parsed, dict):
                description = str(parsed.get("description") or "")
            raise TelegramApiError(description or f"Telegram API call failed: {method}")
        return parsed.get("result")


def chunk_message(text: str, limit: int = 4000) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return [""]
    out: list[str] = []
    remaining = raw
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 3:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 3:
            split_at = limit
        out.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        out.append(remaining)
    return out

