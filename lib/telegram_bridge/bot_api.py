from __future__ import annotations

import html
import json
import mimetypes
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError


_FENCED_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_BOLD_DOUBLE_UNDERSCORE_RE = re.compile(r"__([^_\n]+?)__")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def markdown_to_telegram_html(text: str) -> str:
    """Convert common Markdown to Telegram's HTML subset.

    We send with `parse_mode=HTML` rather than legacy Markdown because
    HTML treats stray `_` and `*` as plain characters. Legacy Markdown
    used to choke on identifiers like `bot_token` or `claude_code_bridge`
    in prose: it'd read the first underscore as opening italic, fail to
    find a close, return 400 'can't parse entities', and our send_message
    wrapper would fall back to RAW text (showing `**bold**` literally).
    HTML mode eliminates that whole class of failure.

    Conversions:
        `**bold**` / `__bold__`        → `<b>...</b>`
        `` `code` ``                    → `<code>...</code>`
        ```` ```fenced``` ````          → `<pre>...</pre>`
            (with optional language tag → `<pre><code class="language-X">...</code></pre>`)
        `[label](url)`                  → `<a href="url">label</a>`

    Italic conversion is intentionally skipped — bare `_` / `*` in
    identifiers is too common to safely italicize.
    """
    if not text:
        return text

    # 1. Escape `<`, `>`, `&` first so user-supplied prose can't break HTML.
    #    The markdown markers (`*`, `_`, `` ` ``, `[`, `]`, `(`, `)`) are
    #    untouched by html.escape, so the regex passes below still match.
    text = html.escape(text, quote=False)

    # 2. Code blocks first (so a `**` inside a code fence doesn't get
    #    converted to <b>). Body is already HTML-escaped from step 1.
    def _fenced(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        body = m.group(2).rstrip()
        if lang:
            return f'<pre><code class="language-{lang}">{body}</code></pre>'
        return f"<pre>{body}</pre>"

    text = _FENCED_RE.sub(_fenced, text)
    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", text)

    # 3. Bold (skip italic — too many false positives on identifiers).
    text = _BOLD_DOUBLE_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_DOUBLE_UNDERSCORE_RE.sub(r"<b>\1</b>", text)

    # 4. Inline links. URL `&` already escaped to `&amp;` by step 1,
    #    which is correct inside an HTML attribute (Telegram un-escapes
    #    on parse). Quote chars in URLs are rare; if they occur the
    #    attribute breaks, but the rest of the message still delivers.
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    return text


# Back-compat alias. Kept so other code that imported the old name still
# works during the rollout — the function now returns HTML instead of
# Markdown, and callers must use parse_mode=HTML to match.
to_telegram_markdown = markdown_to_telegram_html


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

    def send_photo(
        self,
        chat_id: str | int,
        file_path: str | Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Upload a local image file via multipart/form-data → sendPhoto.

        Caption is rendered through markdown_to_telegram_html so prefixes
        like `[Grok]` survive consistently with text replies. On parse
        failure, retry without parse_mode (same fallback as send_message).
        """
        return self._send_media(
            method="sendPhoto",
            chat_id=chat_id,
            field_name="photo",
            file_path=file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )

    def send_video(
        self,
        chat_id: str | int,
        file_path: str | Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        return self._send_media(
            method="sendVideo",
            chat_id=chat_id,
            field_name="video",
            file_path=file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )

    def send_document(
        self,
        chat_id: str | int,
        file_path: str | Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        return self._send_media(
            method="sendDocument",
            chat_id=chat_id,
            field_name="document",
            file_path=file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )

    def _send_media(
        self,
        *,
        method: str,
        chat_id: str | int,
        field_name: str,
        file_path: str | Path,
        caption: str,
        reply_to_message_id: int | None,
    ) -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise TelegramApiError(f"Media file not found: {path}")

        fields: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = markdown_to_telegram_html(caption)
            fields["parse_mode"] = "HTML"
        if reply_to_message_id:
            fields["reply_to_message_id"] = str(int(reply_to_message_id))

        try:
            return self._call_multipart(method, fields, field_name, path)
        except TelegramApiError as exc:
            msg = str(exc).lower()
            if "parse" in msg or "entity" in msg or "can't find end" in msg or "unsupported" in msg:
                fields.pop("parse_mode", None)
                fields["caption"] = caption or ""
                return self._call_multipart(method, fields, field_name, path)
            raise

    def _call_multipart(
        self,
        method: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> Any:
        boundary = "----CCBTG" + secrets.token_hex(16)
        body = self._build_multipart(boundary, fields, file_field, file_path)
        url = f"{self.base_url}/{method}"
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.http_timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            resp_body = exc.read().decode("utf-8", errors="replace")
            raise TelegramApiError(f"HTTP {exc.code}: {resp_body}") from exc
        except URLError as exc:
            raise TelegramApiError(f"Network error: {exc}") from exc
        except Exception as exc:
            raise TelegramApiError(f"Telegram API request failed: {exc}") from exc

        try:
            parsed = json.loads(resp_body)
        except json.JSONDecodeError as exc:
            raise TelegramApiError(f"Bad JSON from Telegram: {exc}: {resp_body[:200]}") from exc
        if not parsed.get("ok"):
            raise TelegramApiError(f"Telegram error: {parsed.get('description', resp_body)}")
        return parsed.get("result")

    @staticmethod
    def _build_multipart(
        boundary: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> bytes:
        # Stdlib-only multipart/form-data assembly. Telegram is tolerant of
        # missing Content-Length on parts, so we keep this simple.
        crlf = b"\r\n"
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(b"--" + boundary.encode())
            parts.append(crlf)
            parts.append(
                f'Content-Disposition: form-data; name="{name}"'.encode()
            )
            parts.append(crlf + crlf)
            parts.append(str(value).encode("utf-8"))
            parts.append(crlf)
        # File part
        filename = file_path.name
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"
        parts.append(b"--" + boundary.encode())
        parts.append(crlf)
        parts.append(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
        )
        parts.append(crlf)
        parts.append(f"Content-Type: {mime}".encode())
        parts.append(crlf + crlf)
        parts.append(file_path.read_bytes())
        parts.append(crlf)
        parts.append(b"--" + boundary.encode() + b"--" + crlf)
        return b"".join(parts)

    def send_message(self, chat_id: str | int, text: str, *, reply_to_message_id: int | None = None) -> dict[str, Any]:
        original = text or ""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": markdown_to_telegram_html(original),
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        try:
            return self._call("sendMessage", payload)
        except TelegramApiError as exc:
            msg = str(exc).lower()
            if "parse" in msg or "entity" in msg or "can't find end" in msg or "unsupported" in msg:
                # Malformed HTML (rare — converter is conservative) — retry
                # as plain text so the message at least gets through.
                payload.pop("parse_mode", None)
                payload["text"] = original
                return self._call("sendMessage", payload)
            raise

    def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
    ) -> dict[str, Any] | None:
        """editMessageText. Returns None if Telegram rejects (e.g. message gone)."""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": markdown_to_telegram_html(text or ""),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            return self._call("editMessageText", payload)
        except TelegramApiError as exc:
            msg = str(exc).lower()
            if "parse" in msg or "entity" in msg or "can't find end" in msg or "unsupported" in msg:
                payload.pop("parse_mode", None)
                payload["text"] = text or ""
                try:
                    return self._call("editMessageText", payload)
                except TelegramApiError:
                    return None
            # "message is not modified" is benign — content unchanged.
            if "not modified" in msg or "message to edit not found" in msg:
                return None
            return None

    def delete_message(self, chat_id: str | int, message_id: int) -> None:
        """deleteMessage. Errors are swallowed (best-effort cleanup)."""
        try:
            self._call("deleteMessage", {
                "chat_id": str(chat_id),
                "message_id": int(message_id),
            })
        except TelegramApiError:
            pass

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
            "text": markdown_to_telegram_html(text or ""),
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
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
            if "parse" in msg or "entity" in msg or "can't find end" in msg or "unsupported" in msg:
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

