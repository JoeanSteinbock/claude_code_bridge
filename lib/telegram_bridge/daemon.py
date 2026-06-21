from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Optional

from . import _mount
from .bot_api import TelegramApiError, TelegramBotClient, chunk_message
from .config import SUPPORTED_PROVIDERS, TelegramConfig, get_config_dir, get_project_root, is_configured, load_config
from .router import help_text, parse_message
from project_id import compute_ccb_project_id
from session_utils import find_project_session_file

STATE_FILE = "telegramd.json"
PID_FILE = "telegramd.pid"
LOG_FILE = "telegramd.log"
SESSION_FILES = {
    "claude": ".claude-session",
    "codex": ".codex-session",
    "gemini": ".gemini-session",
    "opencode": ".opencode-session",
    "droid": ".droid-session",
    "copilot": ".copilot-session",
    "codebuddy": ".codebuddy-session",
    "qwen": ".qwen-session",
    "grok": ".grok-session",
}

# Matches the `[Provider]` prefix our bot prepends to every reply
# (see _run_request). Used to infer routing when a user taps reply.
_PROVIDER_PREFIX_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*)\]")

# Grok writes image_gen / image_edit / video_gen outputs under
# ~/.grok/sessions/<encoded-cwd>/<session>/{images,videos}/N.{ext}
# and prints the path verbatim in its reply. Telegramd reads `ask grok`
# stdout in sync mode (bypassing the completion hook's attachment plumb),
# so we re-extract here and sendPhoto/sendVideo each match. Mirror of
# _GROK_MEDIA_RE in lib/askd/adapters/grok.py — keep in sync if updating.
_GROK_MEDIA_RE = re.compile(
    r"(?P<path>/(?:home/[^/\s]+|root)/\.grok/sessions/[^\s`)]+/(?:images|videos)/[^\s`)]+\.(?:jpg|jpeg|png|webp|gif|mp4|webm|mov))",
    re.IGNORECASE,
)


def _extract_grok_media(text: str) -> list[str]:
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _GROK_MEDIA_RE.finditer(text):
        path = m.group("path").rstrip(".,;:!?")
        if path not in seen and Path(path).is_file():
            seen[path] = None
    return list(seen.keys())


def _looks_like_single_emoji(text: str) -> bool:
    """True iff `text` is plausibly just one emoji (single-codepoint or
    multi-codepoint with ZWJ / variation selectors). Used to decide
    whether to deliver a bot reply as a Telegram reaction instead of a
    regular text message."""
    s = (text or "").strip()
    if not s or len(s) > 10:
        return False
    # Any ASCII letter / digit / common punctuation → not a bare emoji.
    for ch in s:
        if ch.isascii() and (ch.isalnum() or ch in "!?.,:;'\"()[]{}<>@#$%^&*_+-=\\|/"):
            return False
    return True


def _match_provider_prefix(body: str) -> str | None:
    if not body:
        return None
    m = _PROVIDER_PREFIX_RE.match(body.lstrip())
    if not m:
        return None
    candidate = m.group(1).lower()
    return candidate if candidate in SUPPORTED_PROVIDERS else None


def _provider_from_replied_to(msg: dict | None) -> str | None:
    """If the user replied to (or quoted) one of our `[Provider]` messages,
    return that provider. Checks `reply_to_message`, `external_reply`
    (cross-chat reply, Telegram 2023+), and `quote` (the user-highlighted
    snippet) — any of which may carry the prefix depending on which
    Telegram client UI the user used."""
    if not isinstance(msg, dict):
        return None

    # Same-chat reply: full original Message object.
    rtm = msg.get("reply_to_message")
    if isinstance(rtm, dict):
        body = str(rtm.get("text", "") or rtm.get("caption", "") or "")
        provider = _match_provider_prefix(body)
        if provider:
            return provider

    # Cross-chat / external reply (newer Telegram feature).
    ext = msg.get("external_reply")
    if isinstance(ext, dict):
        provider = _match_provider_prefix(str(ext.get("text", "") or ""))
        if provider:
            return provider

    # User-highlighted quote snippet (may not include the `[Provider]`
    # prefix if the user only quoted a portion mid-message — that's
    # fine, falls through to None).
    quote = msg.get("quote")
    if isinstance(quote, dict):
        provider = _match_provider_prefix(str(quote.get("text", "") or ""))
        if provider:
            return provider

    return None


def _format_sender(user: dict | None) -> str:
    """Render a Telegram User as `@username` or `First Last`."""
    if not isinstance(user, dict):
        return ""
    username = (user.get("username") or "").strip()
    if username:
        return f"@{username}"
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or ""


def _format_origin(origin: dict | None) -> str:
    """Render a MessageOrigin (forward_origin / external_reply.origin)."""
    if not isinstance(origin, dict):
        return ""
    otype = (origin.get("type") or "").strip()
    if otype == "user":
        return _format_sender(origin.get("sender_user"))
    if otype == "hidden_user":
        return (origin.get("sender_user_name") or "").strip()
    if otype in ("chat", "channel"):
        chat = origin.get("sender_chat") or origin.get("chat") or {}
        username = (chat.get("username") or "").strip()
        if username:
            return f"@{username}"
        return (chat.get("title") or "").strip() or otype
    return ""


def _extract_reply_context(msg: dict) -> str:
    """Build a header describing the reply / quote / forward context so
    the model sees the conversational frame the user is responding to.
    Telegram only puts the user's own text in `msg.text`; the message
    they're replying to or quoting lives in sibling fields and is
    invisible to the provider unless we surface it explicitly."""
    if not isinstance(msg, dict):
        return ""
    parts: list[str] = []

    # Whole-message forward — `msg.text` already carries the body, so
    # we only need to annotate the source.
    fwd = msg.get("forward_origin")
    if isinstance(fwd, dict):
        src = _format_origin(fwd) or "unknown"
        parts.append(f"[forwarded from {src}]")

    # Quote-reply: a snippet from the replied message that the user
    # explicitly highlighted (Telegram 2023+ "reply with quote").
    quote = msg.get("quote")
    quote_text = ""
    if isinstance(quote, dict):
        quote_text = (quote.get("text") or "").strip()
        if quote_text:
            parts.append(f"[quoted snippet] {quote_text}")

    # Same-chat reply. If a quote snippet was provided, the full body
    # would just be noise — keep the attribution but skip the body.
    rtm = msg.get("reply_to_message")
    if isinstance(rtm, dict):
        sender = _format_sender(rtm.get("from")) or "someone"
        body = (rtm.get("text") or rtm.get("caption") or "").strip()
        if quote_text:
            parts.append(f"[replying to {sender}]")
        elif body:
            parts.append(f"[replying to {sender}] {body}")

    # Cross-chat reply (newer Telegram feature). `external_reply` carries
    # an origin + optional content snippet from the other chat.
    ext = msg.get("external_reply")
    if isinstance(ext, dict):
        src = _format_origin(ext.get("origin")) or "another chat"
        body = (ext.get("text") or "").strip()
        if body:
            parts.append(f"[replying to {src}] {body}")
        else:
            parts.append(f"[replying to {src}]")

    return "\n".join(parts)


@dataclass
class DaemonState:
    pid: int
    started_at: float
    status: str = "running"
    bot_username: str = ""
    last_update_id: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DaemonState":
        return cls(
            pid=int(data.get("pid", 0) or 0),
            started_at=float(data.get("started_at", 0.0) or 0.0),
            status=str(data.get("status", "running") or "running"),
            bot_username=str(data.get("bot_username", "") or "").strip(),
            last_update_id=int(data.get("last_update_id", 0) or 0),
        )


def _state_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / STATE_FILE


def _pid_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / PID_FILE


def _log_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / LOG_FILE


def _write_log(line: str, work_dir: str | Path | None = None) -> None:
    path = _log_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def read_daemon_state(work_dir: str | Path | None = None) -> Optional[DaemonState]:
    path = _state_path(work_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return DaemonState.from_dict(data)


def write_daemon_state(state: DaemonState, work_dir: str | Path | None = None) -> None:
    path = _state_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    _pid_path(work_dir).write_text(str(state.pid), encoding="utf-8")


def remove_daemon_state(work_dir: str | Path | None = None) -> None:
    for path in (_state_path(work_dir), _pid_path(work_dir)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) == 0:
                    return False
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def is_daemon_running(work_dir: str | Path | None = None) -> bool:
    state = read_daemon_state(work_dir)
    if not state:
        return False
    if _is_process_alive(state.pid):
        return True
    remove_daemon_state(work_dir)
    return False


def get_daemon_status(work_dir: str | Path | None = None) -> dict:
    state = read_daemon_state(work_dir)
    if not state or not _is_process_alive(state.pid):
        remove_daemon_state(work_dir)
        return {"running": False}
    return {
        "running": True,
        "pid": state.pid,
        "started_at": state.started_at,
        "uptime": time.time() - state.started_at,
        "bot_username": state.bot_username,
        "last_update_id": state.last_update_id,
    }


class TelegramDaemon:
    def __init__(self, config: Optional[TelegramConfig] = None, work_dir: str | Path | None = None):
        self.project_root = get_project_root(work_dir)
        self.config = config or load_config(self.project_root)
        self.stop_event = Event()
        self.client = TelegramBotClient(self.config.bot_token)
        self.state = DaemonState(pid=os.getpid(), started_at=time.time())
        # Per-(chat_id, provider) coalescing state. When a request is in flight
        # for a given pair, additional messages queue here and are flushed as a
        # single combined ask request when the worker drains the queue.
        self._chat_queues: dict[tuple[str, str], list[dict]] = {}
        self._chat_busy: dict[tuple[str, str], bool] = {}
        self._chat_state_lock = Lock()
        # /mount: per-process registry of clone-done flags keyed by
        # suggested_username. Persisted state lives in pending_mounts.json;
        # this dict only tracks the threading.Event for the corresponding
        # background clone. Empty until the first /mount or until
        # _mount.recover_pending repopulates it at startup.
        self._clone_events: dict[str, Event] = {}

    def start(self) -> None:
        me = self.client.get_me()
        username = ""
        if isinstance(me, dict):
            username = str(me.get("username") or "").strip()
        self.state.bot_username = username
        prior = read_daemon_state(self.project_root)
        if prior and prior.last_update_id:
            self.state.last_update_id = prior.last_update_id
        write_daemon_state(self.state, self.project_root)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Register the `/` autocomplete menu so users get command
        # suggestions in Telegram clients. Best-effort: don't block
        # startup if the network call fails.
        try:
            self.client.set_my_commands([
                {"command": "new", "description": "reset a provider (e.g. /new codex)"},
                {"command": "reset", "description": "alias for /new"},
                {"command": "restart", "description": "alias for /new"},
                {"command": "clear", "description": "alias for /new"},
                {"command": "respawn", "description": "full restart of a provider's CLI"},
                {"command": "relaunch", "description": "alias for /respawn"},
                {"command": "context", "description": "show Claude's context window usage"},
                {"command": "compact", "description": "compact Claude's conversation context"},
                {"command": "status", "description": "show Claude's session status"},
                {"command": "stats", "description": "alias for /status"},
                {"command": "tail", "description": "peek at latest pane output (e.g. /tail codex)"},
                {"command": "usage", "description": "show token/cost usage"},
                {"command": "cost", "description": "show billing breakdown"},
                {"command": "config", "description": "open Claude config dialog"},
                {"command": "model", "description": "show/switch Claude model"},
                {"command": "mcp", "description": "show MCP server status"},
                {"command": "sessions", "description": "list Claude sessions"},
                {"command": "wake", "description": "schedule a future ask (e.g. /wake 5m check BTC price)"},
                {"command": "work", "description": "wake shortcut with work-first imperative (/work codex 30m)"},
                {"command": "mount", "description": "clone a repo + spawn a dedicated bot (manager only)"},
                {"command": "providers", "description": "list available providers"},
                {"command": "help", "description": "show usage"},
            ])
        except Exception as exc:
            _write_log(f"[telegramd] setMyCommands failed: {exc}", self.project_root)

        _write_log(f"[telegramd] started pid={self.state.pid} bot=@{username or 'unknown'}", self.project_root)

        # Re-hydrate /mount in-flight state. Must run before the polling
        # loop so any `update["managed_bot"]` arriving on the first poll
        # finds the threading.Event already registered.
        try:
            _mount.recover_pending(self, self.project_root)
        except Exception as exc:
            _write_log(f"[telegramd] mount recovery failed: {exc}", self.project_root)

        while not self.stop_event.is_set():
            try:
                updates = self.client.get_updates(
                    offset=(self.state.last_update_id + 1) if self.state.last_update_id else None,
                    timeout=self.config.long_poll_timeout_seconds,
                    # Explicit list — Telegram treats `allowed_updates` as
                    # a hard filter, so we must enumerate all currently-
                    # received types plus `managed_bot` (the new opt-in
                    # type from the Managed Bots feature). Daemon today
                    # only dispatches `message` and `managed_bot`, but we
                    # include the others defensively for forward compat.
                    allowed_updates=[
                        "message",
                        "edited_message",
                        "channel_post",
                        "edited_channel_post",
                        "callback_query",
                        "managed_bot",
                    ],
                )
                if not updates:
                    time.sleep(self.config.polling_interval_seconds)
                    continue
                # Dispatch each update in its own daemon thread so a slow
                # synchronous handler (e.g. `/tail` capturing a busy pane,
                # `/respawn` blocking on tmux respawn, a synchronous wake
                # roundtrip) does not stall the polling loop. Ask requests
                # already hand off via `_chat_queue` / `_chat_worker`, so
                # this addition only changes behaviour for the slash-command
                # path. We update `last_update_id` BEFORE spawning so the
                # next long-poll advances even if a handler hangs.
                advanced = False
                for update in updates:
                    try:
                        uid = int(update.get("update_id") or 0)
                        if uid > self.state.last_update_id:
                            self.state.last_update_id = uid
                            advanced = True
                    except Exception:
                        pass
                    Thread(
                        target=self._handle_update_safely,
                        args=(update,),
                        daemon=True,
                        name=f"telegramd-dispatch-{update.get('update_id', '?')}",
                    ).start()
                if advanced:
                    try:
                        write_daemon_state(self.state, self.project_root)
                    except Exception:
                        pass
            except TelegramApiError as exc:
                _write_log(f"[telegramd] api error: {exc}", self.project_root)
                time.sleep(max(2, self.config.polling_interval_seconds))
            except Exception as exc:
                _write_log(f"[telegramd] unexpected error: {exc}", self.project_root)
                time.sleep(max(2, self.config.polling_interval_seconds))

        remove_daemon_state(self.project_root)
        _write_log("[telegramd] stopped", self.project_root)

    def _handle_signal(self, signum, _frame) -> None:
        _write_log(f"[telegramd] received signal {signum}", self.project_root)
        self.stop_event.set()

    def _downloads_dir(self) -> Path:
        """Persistent per-project download dir for Telegram attachments."""
        d = get_config_dir(self.project_root) / "telegram_downloads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # File extensions we consider "voice-like" for auto-transcription.
    _AUDIO_EXTS = {".oga", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac"}

    def _download_attachments(self, msg: dict) -> list[tuple[Path, str]]:
        """Download all supported attachments from a Telegram message.

        Returns a list of (local_path, kind) tuples. `kind` is one of
        "photo", "document", "voice", "audio", "video", "video_note",
        "animation" — used downstream to decide whether to transcribe.
        Silently ignores unsupported types (sticker/location/etc.).
        """
        out: list[tuple[Path, str]] = []
        dest = self._downloads_dir()

        # photo: largest variant last
        photos = msg.get("photo")
        if isinstance(photos, list) and photos:
            biggest = max(photos, key=lambda p: int(p.get("file_size") or 0)) if any(isinstance(p, dict) for p in photos) else None
            if isinstance(biggest, dict):
                file_id = str(biggest.get("file_id") or "")
                if file_id:
                    mid = int(msg.get("message_id") or 0)
                    out.append((
                        self.client.download_file(file_id, dest, preferred_name=f"photo-{mid}.jpg"),
                        "photo",
                    ))

        # document, voice, audio, video, video_note, animation — single file each
        for key in ("document", "voice", "audio", "video", "video_note", "animation"):
            media = msg.get(key)
            if not isinstance(media, dict):
                continue
            file_id = str(media.get("file_id") or "")
            if not file_id:
                continue
            name = str(media.get("file_name") or "").strip()
            out.append((
                self.client.download_file(file_id, dest, preferred_name=name),
                key,
            ))

        return out

    def _whisper_model_path(self) -> Path | None:
        """Resolve which GGML model file to use for voice transcription.

        Resolution order: `CCB_WHISPER_MODEL` env, then the default
        `<install>/models/ggml-small.bin`. Returns None if no model
        file is present.
        """
        env_path = os.environ.get("CCB_WHISPER_MODEL", "").strip()
        if env_path:
            p = Path(env_path).expanduser()
            if p.is_file():
                return p
        # Default: prefer `~/.cache/ccb/whisper-models/` (survives
        # `install.sh` which `rm -rf`s the install prefix). Fall back to
        # legacy `<codex-dual>/models/` if someone still keeps it there.
        model_names = ("ggml-medium.bin", "ggml-small.bin", "ggml-base.bin", "ggml-tiny.bin")
        search_dirs = (
            Path.home() / ".cache" / "ccb" / "whisper-models",
            Path.home() / ".local" / "share" / "codex-dual" / "models",
        )
        for d in search_dirs:
            for name in model_names:
                p = d / name
                if p.is_file():
                    return p
        return None

    def _transcribe_voice(self, src: Path) -> str:
        """Transcribe an audio file via whisper-cli; return "" on any failure.

        Works on .oga/.ogg/.opus/etc. directly — whisper-cli handles
        conversion via its bundled ffmpeg reader. Runs with a hard
        wall-clock timeout so a hung model download can't wedge us.
        """
        model = self._whisper_model_path()
        if not model:
            return ""
        whisper_bin = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
        if not Path(whisper_bin).is_file():
            return ""
        # whisper-cli needs 16kHz mono WAV for best results; let ffmpeg
        # convert first so we don't depend on whisper's built-in decoder
        # (which varies by build).
        ffmpeg = shutil.which("ffmpeg")
        wav_path = src.with_suffix(".wav")
        try:
            if ffmpeg:
                subprocess.run(
                    [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav_path)],
                    check=True, capture_output=True, timeout=60,
                )
                input_path = wav_path
            else:
                input_path = src
            result = subprocess.run(
                [whisper_bin, "-m", str(model), "-f", str(input_path),
                 "-l", "auto", "-nt", "-np", "--output-txt", "-of", str(input_path)],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                _write_log(
                    f"[telegramd] whisper-cli exit={result.returncode}: {result.stderr.strip()[:200]}",
                    self.project_root,
                )
                return ""
            # whisper-cli with -of writes <input_path>.txt
            txt_path = Path(f"{input_path}.txt")
            if txt_path.exists():
                return txt_path.read_text(encoding="utf-8", errors="replace").strip()
            # Fallback: parse stdout.
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            _write_log(f"[telegramd] whisper-cli timed out on {src.name}", self.project_root)
            return ""
        except Exception as exc:
            _write_log(f"[telegramd] whisper-cli error on {src.name}: {exc}", self.project_root)
            return ""
        finally:
            # Best-effort cleanup of intermediate wav; leave transcript .txt.
            try:
                if ffmpeg and wav_path.exists():
                    wav_path.unlink()
            except Exception:
                pass

    def _handle_update_safely(self, update: dict) -> None:
        """Thread entrypoint — never let a handler exception kill the worker.

        The polling loop spawns one of these per update so a slow / blocking
        handler (e.g. /tail capturing a stuck pane) does not stall the loop.
        last_update_id and the persisted state are advanced by the polling
        loop BEFORE this fires, so we don't need to redo that here.
        """
        try:
            self._handle_update(update)
        except Exception as exc:
            _write_log(
                f"[telegramd] dispatch worker error: {exc}",
                self.project_root,
            )

    def _handle_update(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0) or 0)
        if update_id > self.state.last_update_id:
            self.state.last_update_id = update_id
            write_daemon_state(self.state, self.project_root)

        # Managed-bots (the bot creation flow): a `managed_bot` update
        # arrives whenever a user taps a `t.me/newbot/<self>/...` link
        # we sent and completes the 1-tap creation. See `/mount`.
        mb = update.get("managed_bot")
        if isinstance(mb, dict):
            try:
                self._handle_managed_bot(mb)
            except Exception as exc:
                _write_log(f"[telegramd] managed_bot handler error: {exc}", self.project_root)
            return

        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = str(chat.get("id", "") or "").strip()
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            return

        # Telegram chat.type is "private" for 1:1 DMs, "group"/"supergroup" for
        # multi-user. In DMs we skip reply_to_message_id for a natural chat feel;
        # in groups we keep it so threading is obvious when multiple users post.
        chat_type = str(chat.get("type", "") or "").strip().lower()
        is_group = chat_type in {"group", "supergroup", "channel"}
        message_id = int(msg.get("message_id", 0) or 0)
        reply_to = message_id if is_group else 0

        # Text, or caption that accompanies a media attachment.
        text = str(msg.get("text", "") or msg.get("caption", "") or "").strip()

        # Surface reply / quote / forward context. Telegram puts only
        # the user's own typing in `text`; anything they're replying to
        # or quoting lives in sibling fields and is otherwise invisible
        # to the provider. Skip for slash commands so command parsing
        # isn't disturbed.
        if text and not text.startswith("/"):
            reply_ctx = _extract_reply_context(msg)
            if reply_ctx:
                text = f"{reply_ctx}\n\n{text}"

        # Detect and download any attachments. Voice/audio get transcribed
        # via whisper-cli so the provider sees the actual spoken text.
        # Other files (photos, documents, video) are surfaced as
        # `[attachment] <path>` lines so the provider can open them with
        # its own tools. On download failure, we still proceed and
        # annotate the prompt so the bot at least knows the user tried
        # to send something (caption/context isn't lost just because the
        # binary fetch hiccuped).
        attachments: list[tuple[Path, str]] = []
        attachment_error = ""
        try:
            attachments = self._download_attachments(msg)
        except Exception as exc:
            attachment_error = str(exc)
            _write_log(f"[telegramd] attachment error chat={chat_id}: {exc}", self.project_root)
            self._send_text(chat_id, f"⚠️ couldn't fetch attachment: {exc}", reply_to_message_id=reply_to)

        if attachments:
            att_lines: list[str] = []
            for path, kind in attachments:
                is_voice_kind = kind in {"voice", "audio", "video_note"}
                is_audio_ext = path.suffix.lower() in self._AUDIO_EXTS
                if is_voice_kind or is_audio_ext:
                    transcript = self._transcribe_voice(path)
                    if transcript:
                        att_lines.append(f"[voice transcript] {transcript}")
                        _write_log(
                            f"[telegramd] transcribed {path.name}: {transcript[:60]!r}",
                            self.project_root,
                        )
                        continue
                att_lines.append(f"[attachment] {path}")
            atts = "\n".join(att_lines)
            text = f"{text}\n\n{atts}".strip() if text else atts

        if attachment_error:
            # Download failed upstream. Tell the bot the user tried to
            # send something, but don't drop the caption/accompanying
            # text if there was any.
            note = f"[attachment-failed] user tried to send an attachment but download failed: {attachment_error}"
            text = f"{text}\n\n{note}".strip() if text else note

        if not text:
            return

        parsed = parse_message(text, self.config.default_provider)
        if parsed.command == "help":
            self._send_text(chat_id, help_text(self.config.default_provider, self.config.broadcast_providers), reply_to_message_id=reply_to)
            return
        if parsed.command == "providers":
            providers = ", ".join(SUPPORTED_PROVIDERS)
            self._send_text(chat_id, f"Providers: {providers}", reply_to_message_id=reply_to)
            return
        if parsed.command in ("new", "new_all"):
            self._run_new_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "new_usage":
            available = ", ".join(self._available_providers()) or "(none mounted)"
            self._send_text(
                chat_id,
                f"Usage: /new <provider> (or `all`).\nMounted: {available}",
                reply_to_message_id=reply_to,
            )
            return
        if parsed.command in ("respawn", "respawn_all"):
            self._run_respawn_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "respawn_usage":
            available = ", ".join(self._available_providers()) or "(none mounted)"
            self._send_text(
                chat_id,
                f"Usage: /respawn <provider> (or `all`). Full CLI restart.\nMounted: {available}",
                reply_to_message_id=reply_to,
            )
            return
        if parsed.command in ("context", "compact", "status", "usage", "cost",
                              "config", "model", "mcp", "sessions"):
            self._run_slash_passthrough(
                parsed.command, parsed.provider or "claude",
                chat_id, reply_to,
                arg=(parsed.message or "").strip(),
            )
            return
        if parsed.command == "tail":
            self._run_tail_command(parsed.provider or "claude", chat_id, reply_to)
            return
        if parsed.command in ("wake_add", "wake_list", "wake_cancel", "wake_usage"):
            self._run_wake_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "work_add":
            self._run_wake_command(parsed, chat_id, reply_to, work=True)
            return
        if parsed.command in ("mount_add", "mount_usage"):
            self._run_mount_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "work_usage":
            self._send_text(
                chat_id,
                "Usage:\n"
                "  /work <duration> [hint]             — agent defaults to claude\n"
                "  /work <agent> <duration> [hint]     — explicit agent\n\n"
                "Like /wake but with a work-first imperative: the agent is told to make "
                "real edits/tool calls/commits this turn, THEN report, THEN self-schedule "
                "if not done. Prevents report-only lazy replies.\n\n"
                "Example: /work codex 30m PR #29 mobile DOM",
                reply_to_message_id=reply_to,
            )
            return
        if not parsed.message:
            self._send_text(chat_id, "Empty message.", reply_to_message_id=reply_to)
            return

        if parsed.targets:
            # Multi-mention: "@claude ... @codex ..." — deliver the full
            # message to each mentioned provider. They see each other's
            # mentions so they can reason about who else is addressed.
            providers = list(parsed.targets)
            # Silently skip mentioned providers that aren't mounted in
            # this project — user shouldn't get N error messages just
            # because they addressed a model that isn't running.
            mounted_set = set(self._available_providers())
            providers = [p for p in providers if p in mounted_set]
        elif parsed.broadcast:
            # Filter out unmounted providers for the same reason.
            mounted_set = set(self._available_providers())
            providers = [p for p in self.config.broadcast_providers if p in mounted_set]
        else:
            # Precedence: explicit prefix > reply_to target > default_provider.
            inferred = _provider_from_replied_to(msg)
            if not inferred:
                # Diagnostic: log what we saw so a mis-routed reply is
                # debuggable. Truncated to 80 chars per field.
                rtm_body = str((msg.get("reply_to_message") or {}).get("text", "") or
                               (msg.get("reply_to_message") or {}).get("caption", "") or "")[:80]
                ext_body = str((msg.get("external_reply") or {}).get("text", "") or "")[:80]
                qte_body = str((msg.get("quote") or {}).get("text", "") or "")[:80]
                if rtm_body or ext_body or qte_body:
                    _write_log(
                        f"[telegramd] reply-route miss: rtm={rtm_body!r} ext={ext_body!r} quote={qte_body!r}",
                        self.project_root,
                    )
            chosen = parsed.provider or inferred or self.config.default_provider
            providers = [chosen]

        if not providers:
            # Broadcast / multi-mention landed on zero mounted targets.
            # Tell the user once instead of going silent.
            available = ", ".join(self._available_providers()) or "(none)"
            self._send_text(
                chat_id,
                f"No mounted providers matched. Available: {available}",
                reply_to_message_id=reply_to,
            )
            return
        for provider in providers:
            self._enqueue_message(
                provider=provider,
                message=parsed.message,
                chat_id=chat_id,
                is_group=is_group,
                message_id=message_id,
            )

    def _enqueue_message(
        self,
        *,
        provider: str,
        message: str,
        chat_id: str,
        is_group: bool,
        message_id: int,
    ) -> None:
        """Queue a message for (chat_id, provider); start a worker if idle."""
        key = (chat_id, provider)
        item = {"message": message, "message_id": message_id, "is_group": is_group}
        with self._chat_state_lock:
            self._chat_queues.setdefault(key, []).append(item)
            if self._chat_busy.get(key):
                # Worker already running; it will drain this on its next loop.
                return
            self._chat_busy[key] = True
        Thread(
            target=self._chat_worker,
            args=(chat_id, provider),
            daemon=True,
            name=f"telegramd-worker-{chat_id}-{provider}",
        ).start()

    def _chat_worker(self, chat_id: str, provider: str) -> None:
        """Drain the queue for (chat_id, provider), batching while busy."""
        key = (chat_id, provider)
        try:
            while True:
                with self._chat_state_lock:
                    batch = self._chat_queues.get(key) or []
                    self._chat_queues[key] = []
                    if not batch:
                        self._chat_busy[key] = False
                        return
                self._run_batch(provider, chat_id, batch)
        except Exception as exc:
            _write_log(f"[telegramd] worker error chat={chat_id} provider={provider}: {exc}", self.project_root)
            with self._chat_state_lock:
                self._chat_busy[key] = False

    def _run_batch(self, provider: str, chat_id: str, batch: list[dict]) -> None:
        """Combine queued messages into one ask request."""
        if not batch:
            return
        is_group = any(item.get("is_group") for item in batch)
        # In group chats we anchor the reply at the first queued message so users
        # can trace which burst triggered the reply. In DMs we send plain.
        anchor_id = int(batch[0].get("message_id") or 0) if is_group else 0
        # Track the original message id even in DMs so short emoji replies
        # can be delivered as reactions on that message (only meaningful
        # for single-message batches; a coalesced batch of >1 doesn't have
        # one natural "target" message).
        source_message_id = int(batch[0].get("message_id") or 0) if len(batch) == 1 else 0
        if len(batch) == 1:
            combined = str(batch[0].get("message") or "")
        else:
            lines = [
                f"The following {len(batch)} messages arrived back-to-back from the same user. "
                "Treat them as one combined turn and give a single consolidated reply.",
                "",
            ]
            for i, item in enumerate(batch, 1):
                lines.append(f"{i}. {item.get('message') or ''}")
            combined = "\n".join(lines)
        self._run_request(provider, combined, chat_id, anchor_id, source_message_id=source_message_id)

    def _run_request(self, provider: str, message: str, chat_id: str, reply_to_message_id: int, *, source_message_id: int = 0) -> None:
        mounted = self._available_providers()
        if not mounted:
            self._send_text(
                chat_id,
                "CCB is currently offline for this project. No models are mounted right now.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        if provider not in mounted:
            available = ", ".join(mounted)
            self._send_text(
                chat_id,
                f"[{provider.capitalize()}] is not currently mounted for this project.\nAvailable models: {available}",
                reply_to_message_id=reply_to_message_id,
            )
            return

        # Typing indicator loop: Telegram's "... is typing" status expires after
        # ~5s, so keep re-sending sendChatAction every 4s until the reply is
        # ready. This replaces the old "[Provider] processing..." text message.
        typing_stop = Event()

        def _typing_loop() -> None:
            while not typing_stop.is_set():
                try:
                    self.client.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                # Re-send slightly before the 5s expiry so the indicator stays continuous.
                typing_stop.wait(4.0)

        typing_thread: Optional[Thread] = None
        if self.config.send_acknowledgements:
            typing_thread = Thread(target=_typing_loop, name="telegramd-typing", daemon=True)
            typing_thread.start()

        def _stop_typing() -> None:
            if typing_thread is not None:
                typing_stop.set()
                typing_thread.join(timeout=1.0)

        ask_cmd = self._find_ask_command()
        if not ask_cmd:
            _stop_typing()
            self._send_text(chat_id, f"[{provider.capitalize()}] ask command not found", reply_to_message_id=reply_to_message_id)
            return

        work_dir = self._work_dir()
        env = os.environ.copy()
        env["CCB_CALLER"] = "telegram"
        env["CCB_WORK_DIR"] = work_dir
        env["CCB_ASK_EMIT_GUARDRAIL"] = "0"
        env["CCB_UNIFIED_ASKD"] = "1"
        env["CCB_ASKD_AUTOSTART"] = "0"
        # Our own ask invocation captures stdout and posts the reply; the
        # completion hook should NOT also post for this call. Flag it.
        # Nested asks from Claude's pane don't inherit this env var (pane
        # env is set at ccb startup), so their hook post still fires.
        env["CCB_TELEGRAM_SYNC_REPLY"] = "1"
        env.pop("CCB_PARENT_PID", None)
        env.pop("CCB_MANAGED", None)

        # Drop the chat_id in a project-scoped file. Nested asks that
        # Claude fires from inside its pane (the `ask @codex …` delegation
        # pattern) can't read telegramd's env, but the `ask` CLI falls
        # back to this file so the completion hook has somewhere to send
        # the eventual reply. The file is overwritten each turn, so the
        # "active chat" is always the last Telegram sender — fine for the
        # single-user DM setup we have.
        if chat_id:
            try:
                active = self.project_root / ".ccb" / ".active_chat_id"
                active.parent.mkdir(parents=True, exist_ok=True)
                active.write_text(str(chat_id).strip() + "\n")
            except Exception:
                pass
        # Telegramd is a daemon, not a terminal pane. Stripping these prevents
        # `ask` from populating caller_pane_id with whatever pane happened to
        # be in the shell that launched telegramd — which would misroute
        # completion-hook notifications to an unrelated Claude session.
        for _pane_var in ("TMUX_PANE", "WEZTERM_PANE", "CCB_CALLER_PANE_ID", "CCB_CALLER_TERMINAL"):
            env.pop(_pane_var, None)
        try:
            project_id = compute_ccb_project_id(self.project_root)
            if project_id:
                env["CCB_RUN_DIR"] = str(Path.home() / ".cache" / "ccb" / "projects" / project_id[:16])
        except Exception:
            pass

        # Cap per-Telegram-message timeout regardless of config: a stuck
        # ask (provider drift-off-format, pane hang, etc.) should fail fast
        # so the bot's chat worker doesn't head-of-line-block for an hour.
        # Override via `CCB_TELEGRAM_ASK_TIMEOUT_S` env if a specific bot
        # really needs longer-running tasks.
        default_cap = 300
        try:
            cap = int(os.environ.get("CCB_TELEGRAM_ASK_TIMEOUT_S", "") or default_cap)
        except Exception:
            cap = default_cap
        ask_timeout_s = min(self.config.request_timeout_seconds, cap)

        try:
            result = subprocess.run(
                [ask_cmd, provider, "--foreground", "--timeout", str(ask_timeout_s)],
                cwd=work_dir,
                env=env,
                input=message,
                capture_output=True,
                text=True,
                timeout=ask_timeout_s + 30,
            )
        except subprocess.TimeoutExpired:
            _stop_typing()
            self._send_text(chat_id, f"[{provider.capitalize()}] timed out", reply_to_message_id=reply_to_message_id)
            return
        except Exception as exc:
            _stop_typing()
            self._send_text(chat_id, f"[{provider.capitalize()}] failed to start ask: {exc}", reply_to_message_id=reply_to_message_id)
            return

        _stop_typing()

        reply = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        # Prefer sending the captured reply even on non-zero exit. Providers
        # sometimes finish without emitting the protocol done-line (e.g. Claude
        # ends on a tool call); the daemon flags that as exit_code=2 but has
        # already extracted the assistant's text. Delivering that text is more
        # useful to the user than a bare "ask exited with code 2" error.
        if reply:
            # If the reply is essentially just an emoji, try to attach it
            # as a Telegram reaction on the source message instead of
            # sending a whole new message. Falls back to text on any
            # failure (e.g. emoji not in Telegram's allowed set).
            if source_message_id and _looks_like_single_emoji(reply):
                try:
                    self.client.set_message_reaction(chat_id, source_message_id, reply)
                    return
                except Exception as exc:
                    _write_log(
                        f"[telegramd] reaction fallback → text (chat={chat_id}, emoji={reply!r}): {exc}",
                        self.project_root,
                    )
            # Grok image_gen / video_gen path relay: the GrokAdapter writes
            # the absolute file path into the reply text. Sync mode bypasses
            # the completion hook's attachment plumb, so we re-extract here
            # and sendPhoto/sendVideo first, then post the text.
            attachments = _extract_grok_media(reply) if provider == "grok" else []
            for idx, file_path in enumerate(attachments):
                caption = f"[{provider.capitalize()}]" if idx == 0 else ""
                try:
                    path_obj = Path(file_path)
                    ext = path_obj.suffix.lower().lstrip(".")
                    if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
                        self.client.send_photo(chat_id, path_obj, caption=caption, reply_to_message_id=reply_to_message_id if idx == 0 else None)
                    elif ext in {"mp4", "webm", "mov"}:
                        self.client.send_video(chat_id, path_obj, caption=caption, reply_to_message_id=reply_to_message_id if idx == 0 else None)
                    else:
                        self.client.send_document(chat_id, path_obj, caption=caption, reply_to_message_id=reply_to_message_id if idx == 0 else None)
                except Exception as exc:
                    _write_log(
                        f"[telegramd] grok media send failed chat={chat_id} path={file_path}: {exc}",
                        self.project_root,
                    )
            self._send_text(chat_id, f"[{provider.capitalize()}]\n{reply}", reply_to_message_id=reply_to_message_id)
            return
        if result.returncode != 0:
            msg = err or f"ask exited with code {result.returncode}"
            self._send_text(chat_id, f"[{provider.capitalize()}] {msg}", reply_to_message_id=reply_to_message_id)
            return
        self._send_text(chat_id, f"[{provider.capitalize()}] (empty reply)", reply_to_message_id=reply_to_message_id)

    def _send_text(self, chat_id: str, text: str, *, reply_to_message_id: int | None = None) -> None:
        try:
            for chunk in chunk_message(text):
                self.client.send_message(chat_id, chunk, reply_to_message_id=reply_to_message_id)
                reply_to_message_id = None
        except Exception as exc:
            _write_log(f"[telegramd] send error chat={chat_id}: {exc}", self.project_root)

    def _work_dir(self) -> str:
        if self.config.default_work_dir:
            candidate = Path(self.config.default_work_dir).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            return str(candidate)
        return str(self.project_root)

    def _available_providers(self) -> list[str]:
        work_dir = self.project_root
        mounted: list[str] = []
        for provider in SUPPORTED_PROVIDERS:
            session_name = SESSION_FILES.get(provider)
            if not session_name:
                continue
            session_file = find_project_session_file(work_dir, session_name)
            if not session_file or not session_file.exists():
                continue
            try:
                data = json.loads(session_file.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("active") is False:
                continue
            mounted.append(provider)
        return mounted

    def _find_ask_command(self) -> str | None:
        project_root = Path(__file__).resolve().parents[2]
        candidates = [
            project_root / "bin" / "ask",
            Path.home() / ".local" / "bin" / "ask",
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "ask",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    def _find_autonew_command(self) -> str | None:
        project_root = Path(__file__).resolve().parents[2]
        candidates = [
            project_root / "bin" / "autonew",
            Path.home() / ".local" / "bin" / "autonew",
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "autonew",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    def _run_new_command(self, parsed, chat_id: str, reply_to_message_id: int) -> None:
        """Handle `/new <provider>` and `/new all` — reset provider sessions.

        Runs `autonew <provider>` directly (no AI round-trip). Results are
        delivered as a short Telegram reply.
        """
        autonew_cmd = self._find_autonew_command()
        if not autonew_cmd:
            self._send_text(chat_id, "autonew command not found on this host.",
                            reply_to_message_id=reply_to_message_id)
            return

        if parsed.command == "new_all":
            targets = list(self._available_providers())
            if not targets:
                self._send_text(chat_id, "No providers are mounted for this project.",
                                reply_to_message_id=reply_to_message_id)
                return
        else:
            target = (parsed.provider or "").strip().lower()
            if not target or target not in SUPPORTED_PROVIDERS:
                rest = (parsed.message or "").strip() or "(empty)"
                self._send_text(
                    chat_id,
                    f"Unknown provider for /new: {rest!r}. Try one of: "
                    f"{', '.join(SUPPORTED_PROVIDERS)} or 'all'.",
                    reply_to_message_id=reply_to_message_id,
                )
                return
            targets = [target]

        work_dir = self._work_dir()
        env = os.environ.copy()
        env["CCB_WORK_DIR"] = work_dir
        # Don't let inherited pane vars confuse autonew's pane resolver.
        for v in ("TMUX_PANE", "WEZTERM_PANE", "CCB_CALLER_PANE_ID", "CCB_CALLER_TERMINAL"):
            env.pop(v, None)

        results: list[str] = []
        for target in targets:
            try:
                rc = subprocess.run(
                    [autonew_cmd, target],
                    cwd=work_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if rc.returncode == 0:
                    results.append(f"✓ {target}")
                else:
                    msg = (rc.stderr or rc.stdout or "").strip().splitlines()
                    tail = msg[-1] if msg else f"exit {rc.returncode}"
                    results.append(f"✗ {target}: {tail[:120]}")
            except subprocess.TimeoutExpired:
                results.append(f"✗ {target}: timed out")
            except Exception as exc:
                results.append(f"✗ {target}: {exc}")

        self._send_text(chat_id, "Reset:\n" + "\n".join(results),
                        reply_to_message_id=reply_to_message_id)

    def _run_respawn_command(self, parsed, chat_id: str, reply_to_message_id: int) -> None:
        """Kill + relaunch a provider's CLI in its tmux/wezterm pane.

        Full process restart (new PID), not just a `/new` inside the CLI.
        Reads pane_id + start_cmd from the provider's session file and
        calls the terminal backend's `respawn_pane`.
        """
        if parsed.command == "respawn_all":
            targets = list(self._available_providers())
            if not targets:
                self._send_text(chat_id, "No providers are mounted for this project.",
                                reply_to_message_id=reply_to_message_id)
                return
        else:
            target = (parsed.provider or "").strip().lower()
            if not target or target not in SUPPORTED_PROVIDERS:
                rest = (parsed.message or "").strip() or "(empty)"
                self._send_text(
                    chat_id,
                    f"Unknown provider for /respawn: {rest!r}. Try one of: "
                    f"{', '.join(SUPPORTED_PROVIDERS)} or 'all'.",
                    reply_to_message_id=reply_to_message_id,
                )
                return
            targets = [target]

        work_dir = self._work_dir()
        # Import lazily to avoid pulling terminal backends on startup.
        try:
            from terminal import get_backend_for_session
        except Exception as exc:
            self._send_text(chat_id, f"Could not load terminal backend: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        results: list[str] = []
        for target in targets:
            session_name = SESSION_FILES.get(target)
            if not session_name:
                results.append(f"✗ {target}: no session file mapping")
                continue
            session_file = find_project_session_file(self.project_root, session_name)
            if not session_file or not session_file.exists():
                results.append(f"✗ {target}: no active session")
                continue
            try:
                data = json.loads(session_file.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                results.append(f"✗ {target}: unreadable session ({exc})")
                continue
            pane_id = str(data.get("pane_id") or "").strip()
            start_cmd = str(data.get("start_cmd") or "").strip()
            if not (pane_id and start_cmd):
                results.append(f"✗ {target}: missing pane_id or start_cmd")
                continue
            try:
                backend = get_backend_for_session(data)
                if not backend or not hasattr(backend, "respawn_pane"):
                    results.append(f"✗ {target}: backend lacks respawn_pane")
                    continue
                backend.respawn_pane(pane_id, cmd=start_cmd, cwd=work_dir, remain_on_exit=True)
                results.append(f"✓ {target} (pane {pane_id})")
            except Exception as exc:
                results.append(f"✗ {target}: {exc}")

        self._send_text(chat_id, "Respawn:\n" + "\n".join(results),
                        reply_to_message_id=reply_to_message_id)

    def _run_wake_command(self, parsed, chat_id: str, reply_to_message_id: int, *, work: bool = False) -> None:
        """Schedule / list / cancel wakes from Telegram.

        Shells out to the existing `wake` CLI instead of reimplementing
        the queue format — keeps parity with what models and the terminal
        shell already do. CCB_WORK_DIR is pinned to this project so the
        queue file ends up at <project>/.ccb/wake_queue.json.
        """
        wake_cmd = shutil.which("wake") or str(
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "wake"
        )
        if not Path(wake_cmd).is_file():
            self._send_text(chat_id, "wake command not found on this host.",
                            reply_to_message_id=reply_to_message_id)
            return

        env = os.environ.copy()
        env["CCB_WORK_DIR"] = self._work_dir()

        if parsed.command == "wake_usage":
            self._send_text(
                chat_id,
                "Usage:\n"
                "  /wake <duration> <message>             — agent defaults to claude\n"
                "  /wake <agent> <duration> <message>     — explicit agent\n"
                "  /wake list                             — show pending\n"
                "  /wake cancel <wake_id>                 — remove one\n\n"
                "Duration: 30s | 5m | 1h | 1h30m\n"
                "Example: /wake 15m check BTC price + summarize",
                reply_to_message_id=reply_to_message_id,
            )
            return

        if parsed.command == "wake_list":
            try:
                out = subprocess.run(
                    [wake_cmd, "list"], cwd=self._work_dir(), env=env,
                    capture_output=True, text=True, timeout=10,
                )
            except Exception as exc:
                self._send_text(chat_id, f"wake list failed: {exc}",
                                reply_to_message_id=reply_to_message_id)
                return
            body = (out.stdout or "").strip() or "(no pending wakes)"
            self._send_text(chat_id, f"```\n{body}\n```",
                            reply_to_message_id=reply_to_message_id)
            return

        if parsed.command == "wake_cancel":
            wake_id = (parsed.message or "").strip().split()[0] if parsed.message else ""
            if not wake_id:
                self._send_text(chat_id, "Usage: /wake cancel <wake_id>",
                                reply_to_message_id=reply_to_message_id)
                return
            try:
                out = subprocess.run(
                    [wake_cmd, "cancel", wake_id], cwd=self._work_dir(), env=env,
                    capture_output=True, text=True, timeout=10,
                )
            except Exception as exc:
                self._send_text(chat_id, f"wake cancel failed: {exc}",
                                reply_to_message_id=reply_to_message_id)
                return
            body = (out.stdout or out.stderr or "").strip() or f"rc={out.returncode}"
            self._send_text(chat_id, body, reply_to_message_id=reply_to_message_id)
            return

        # wake_add
        agent = (parsed.provider or "claude").strip().lower()
        # parsed.message is "<duration> <message>"; split off duration.
        parts = (parsed.message or "").split(None, 1)
        if len(parts) < 2:
            self._send_text(chat_id, "Usage: /wake <duration> <message>",
                            reply_to_message_id=reply_to_message_id)
            return
        duration, message = parts[0].strip(), parts[1].strip()
        if not message:
            self._send_text(chat_id, "Message required.",
                            reply_to_message_id=reply_to_message_id)
            return
        try:
            cmd_args = [wake_cmd, "add", agent, "--in", duration,
                        "--caller", "telegram", "--chat-id", str(chat_id)]
            if work:
                cmd_args.append("--work")
            cmd_args.append(message)
            out = subprocess.run(
                cmd_args,
                cwd=self._work_dir(), env=env,
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            self._send_text(chat_id, f"wake add failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return
        if out.returncode != 0:
            err = (out.stderr or out.stdout or "").strip() or f"rc={out.returncode}"
            self._send_text(chat_id, f"wake: {err}",
                            reply_to_message_id=reply_to_message_id)
            return
        wake_id = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        self._send_text(
            chat_id,
            f"⏰ Wake scheduled: `{wake_id}`\n"
            f"{agent} in {duration} → will reply here when it fires.",
            reply_to_message_id=reply_to_message_id,
        )

    # ------------------------------------------------------------------
    # /mount — clone repo + spawn dedicated bot.
    # See `_mount.py` for helpers and the recovery dance on daemon
    # restart. The slash handler MUST persist pending state and register
    # the in-memory Event BEFORE starting the clone thread or sending
    # the inline keyboard, otherwise a fast user tap can race the JSON
    # write and `_handle_managed_bot` fails to find the entry.
    # ------------------------------------------------------------------

    def _run_mount_command(self, parsed, chat_id: str, reply_to_message_id: int) -> None:
        if parsed.command == "mount_usage":
            self._send_text(
                chat_id,
                "Usage: `/mount <git-url>`\n"
                "Supported: github.com / gitlab.com / bitbucket.org (https or ssh).",
                reply_to_message_id=reply_to_message_id,
            )
            return

        url = (parsed.message or "").strip()
        parsed_url = _mount.parse_repo_url(url)
        if not parsed_url:
            self._send_text(
                chat_id,
                "URL must be github.com / gitlab.com / bitbucket.org (https or ssh).",
                reply_to_message_id=reply_to_message_id,
            )
            return
        owner, repo = parsed_url
        target = _mount.target_path(owner, repo)
        suggested = _mount.suggested_username(owner, repo)
        manager_username = (self.state.bot_username or "").strip()
        if not manager_username:
            self._send_text(
                chat_id,
                "Manager bot username unknown — can't build deep link. Try again in a moment.",
                reply_to_message_id=reply_to_message_id,
            )
            return

        # ORDER MATTERS: persist pending + register Event BEFORE clone
        # thread spawn and BEFORE sending the keyboard, so a fast tap
        # can never race ahead of the JSON write.
        _mount.add_pending(self.project_root, suggested, chat_id, str(target), url)
        done_event = Event()
        self._clone_events[suggested] = done_event

        Thread(
            target=self._clone_repo_bg,
            args=(url, target, suggested, done_event),
            daemon=True,
            name=f"ccb-mount-clone-{suggested}",
        ).start()

        button_url = (
            f"https://t.me/newbot/{manager_username}/{suggested}"
            f"?name={owner}/{repo}"
        )
        # Telegram's Markdown can't render `@` plus underscore-heavy
        # bot names cleanly inside a link, so the body stays terse and
        # the URL lives in the inline button.
        self.client.send_message_with_url_button(
            chat_id,
            f"📦 Cloning `{owner}/{repo}` → `{target}`.\n"
            f"Tap below to create `@{suggested}`. Once it's live, "
            "I'll wire it up automatically.",
            button_text=f"Create @{suggested}",
            button_url=button_url,
            reply_to_message_id=reply_to_message_id,
        )

    def _clone_repo_bg(
        self,
        url: str,
        target: Path,
        suggested: str,
        done_event: Event,
    ) -> None:
        """Run `git clone` (or fetch) off the polling thread. Always
        sets `done_event` so `_handle_managed_bot` unblocks even on
        clone failure — error reporting happens at handoff time."""
        try:
            ok, msg = _mount.clone_repo(url, target)
            _write_log(
                f"[telegramd] mount clone {suggested}: {'ok' if ok else 'FAIL'} {msg}",
                self.project_root,
            )
        finally:
            done_event.set()

    def _handle_managed_bot(self, mb: dict) -> None:
        bot = mb.get("bot") or {}
        bot_id = int(bot.get("id") or 0)
        bot_username = (bot.get("username") or "").lower()
        if not (bot_id and bot_username):
            _write_log(
                f"[telegramd] managed_bot missing bot.id or bot.username: {mb!r}",
                self.project_root,
            )
            return

        pending = _mount.pop_pending(self.project_root, bot_username)
        if not pending:
            # Could be a bot we already processed, or the user picked a
            # different username than we suggested. Log + bail.
            _write_log(
                f"[telegramd] managed_bot for unknown @{bot_username} (id={bot_id}); ignoring",
                self.project_root,
            )
            return

        chat_id = pending.get("chat_id", "")
        target = pending.get("target", "")

        ev = self._clone_events.get(bot_username)
        if ev is not None and not ev.wait(timeout=60):
            self._send_text(
                chat_id,
                f"⏳ Clone for @{bot_username} still running after 60s — "
                "will keep going in the background. Re-tap the button "
                "to retry once it's done.",
            )
            # Re-add so the next tap finds the entry.
            _mount.add_pending(
                self.project_root, bot_username, chat_id, target, pending.get("url", "")
            )
            return

        try:
            token = self.client.get_managed_bot_token(user_id=bot_id)
        except TelegramApiError as exc:
            # Log bot.id explicitly so a future /mount-recover can use it
            # without scraping the Telegram side.
            _write_log(
                f"[telegramd] getManagedBotToken failed for @{bot_username} "
                f"(bot_id={bot_id}, chat={chat_id}): {exc}",
                self.project_root,
            )
            self._send_text(
                chat_id,
                f"❌ Token fetch for @{bot_username} failed: {exc}\n"
                f"Bot exists on Telegram (id={bot_id}); manual recovery in Phase 2.",
            )
            return

        if not token:
            self._send_text(
                chat_id,
                f"❌ getManagedBotToken returned empty for @{bot_username} (id={bot_id}).",
            )
            return

        try:
            rc = subprocess.run(
                ["ccb", "mount", target, "--bot-token", token, "--chat-id", str(chat_id)],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            self._send_text(chat_id, f"❌ `ccb mount` spawn failed: {exc}")
            return

        if rc.returncode != 0:
            err = (rc.stderr or rc.stdout or "").strip() or f"rc={rc.returncode}"
            self._send_text(chat_id, f"❌ `ccb mount` failed:\n```\n{err[:1500]}\n```")
            return

        self._send_text(
            chat_id,
            f"✅ @{bot_username} live at `{target}`.\n"
            f"DM it to start working in that project.",
        )

    def _run_tail_command(
        self,
        provider: str,
        chat_id: str,
        reply_to_message_id: int,
    ) -> None:
        """Snapshot the provider's tmux pane and post the tail back.

        Read-only — doesn't type anything into the pane. Useful for
        checking on a long-running turn remotely without interrupting it.
        """
        provider = (provider or "claude").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            self._send_text(chat_id, f"Unknown provider: {provider}",
                            reply_to_message_id=reply_to_message_id)
            return
        session_filename = SESSION_FILES.get(provider)
        if not session_filename:
            self._send_text(chat_id, f"No session-file mapping for provider {provider}.",
                            reply_to_message_id=reply_to_message_id)
            return
        session_file = find_project_session_file(self.project_root, session_filename)
        if not session_file or not session_file.exists():
            self._send_text(chat_id, f"{provider} is not mounted for this project.",
                            reply_to_message_id=reply_to_message_id)
            return

        try:
            data = json.loads(session_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self._send_text(chat_id, f"Couldn't read {provider} session: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        terminal = (data.get("terminal") or "tmux").strip().lower()
        pane_id = str(data.get("pane_id") or data.get("tmux_session") or "").strip()
        if not pane_id:
            self._send_text(chat_id, f"{provider} session has no pane_id.",
                            reply_to_message_id=reply_to_message_id)
            return
        if terminal != "tmux":
            self._send_text(
                chat_id,
                f"/tail is tmux-only for now (this pane is {terminal}).",
                reply_to_message_id=reply_to_message_id,
            )
            return

        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-80"],
                check=True, capture_output=True, text=True,
            )
            tail = (cap.stdout or "").rstrip()
        except Exception as exc:
            self._send_text(chat_id, f"tmux capture-pane failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        # Collapse trailing blank lines; Telegram markdown hates huge blobs,
        # so cap at ~3500 chars (covers both message limit + code-fence
        # overhead). Preserve the MOST RECENT content by trimming head.
        lines = [ln.rstrip() for ln in tail.split("\n")]
        while lines and not lines[-1].strip():
            lines.pop()
        body = "\n".join(lines)
        if len(body) > 3500:
            body = "…(older output truncated)\n" + body[-3400:]

        if not body:
            body = "(pane is empty)"

        self._send_text(
            chat_id,
            f"[{provider.capitalize()} tail]\n```\n{body}\n```",
            reply_to_message_id=reply_to_message_id,
        )

    def _run_slash_passthrough(
        self,
        command: str,
        provider: str,
        chat_id: str,
        reply_to_message_id: int,
        arg: str = "",
    ) -> None:
        """Type a provider-native slash command (`/context`, `/compact`) into
        the pane and return the output tail.

        Bypasses the ask/CCB_DONE protocol because these commands don't
        follow our reply convention — they produce UI output inside the
        provider's CLI. We snapshot the pane before sending, wait a few
        seconds, capture the post-send tail, and post the diff.
        """
        provider = (provider or "claude").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            self._send_text(chat_id, f"Unknown provider: {provider}",
                            reply_to_message_id=reply_to_message_id)
            return

        session_filename = SESSION_FILES.get(provider)
        if not session_filename:
            self._send_text(chat_id, f"No session-file mapping for provider {provider}.",
                            reply_to_message_id=reply_to_message_id)
            return
        session_file = find_project_session_file(self.project_root, session_filename)
        if not session_file or not session_file.exists():
            self._send_text(chat_id, f"{provider} is not mounted for this project.",
                            reply_to_message_id=reply_to_message_id)
            return

        try:
            data = json.loads(session_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self._send_text(chat_id, f"Couldn't read {provider} session: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        terminal = (data.get("terminal") or "tmux").strip().lower()
        pane_id = str(data.get("pane_id") or data.get("tmux_session") or "").strip()
        if not pane_id:
            self._send_text(chat_id, f"{provider} session has no pane_id.",
                            reply_to_message_id=reply_to_message_id)
            return

        if terminal != "tmux":
            self._send_text(
                chat_id,
                f"/{command} passthrough is tmux-only for now "
                f"(this pane is {terminal}).",
                reply_to_message_id=reply_to_message_id,
            )
            return

        # /compact can take a while (Claude summarizes the whole session);
        # /context is instant. Tune the wait accordingly so we catch the
        # output without blocking the telegramd worker for too long.
        wait_s = 15 if command == "compact" else 4

        try:
            # Send the slash command + Enter into the Claude pane.
            full_cmd = f"/{command} {arg}".strip() if arg else f"/{command}"
            subprocess.run(["tmux", "send-keys", "-t", pane_id, full_cmd],
                           check=True, capture_output=True)
            # Small delay so the CLI registers the line before we press Enter.
            time.sleep(0.2)
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"],
                           check=True, capture_output=True)
        except Exception as exc:
            self._send_text(chat_id, f"tmux send-keys failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        time.sleep(wait_s)

        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-80"],
                check=True, capture_output=True, text=True,
            )
            tail = (cap.stdout or "").rstrip()
        except Exception as exc:
            self._send_text(chat_id, f"tmux capture-pane failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        # Dismiss modal dialogs that Claude Code's /status, /context, /compact
        # pop into the pane. They block the prompt until the user presses
        # Escape. Without this the next ask() would send its text INTO the
        # modal instead of the prompt field. Send Escape a couple times to
        # be safe (covers multi-level nested modals).
        try:
            for _ in range(2):
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"],
                               check=False, capture_output=True)
                time.sleep(0.1)
        except Exception:
            pass

        # Trim leading box-drawing / UI chrome noise: try to find the last
        # occurrence of `/command` we just typed and keep everything below.
        marker = f"/{command}"
        idx = tail.rfind(marker)
        body = tail[idx:].strip() if idx >= 0 else tail
        if len(body) > 3500:
            body = body[:3500] + "\n…(truncated)"

        self._send_text(
            chat_id,
            f"[{provider.capitalize()} /{command}]\n```\n{body}\n```",
            reply_to_message_id=reply_to_message_id,
        )


def start_daemon(foreground: bool = False, work_dir: str | Path | None = None) -> None:
    project_root = get_project_root(work_dir)
    config = load_config(project_root)
    if not config.enabled:
        print("Telegram bridge is disabled. Configure it and enable it first.")
        sys.exit(1)
    if not is_configured(config, project_root):
        print("Telegram bridge is not configured. Set a bot token first.")
        sys.exit(1)
    if is_daemon_running(project_root):
        print("Telegram daemon is already running.")
        return

    daemon = TelegramDaemon(config, project_root)
    if foreground:
        daemon.start()
        return

    code_root = Path(__file__).resolve().parents[2]
    log_path = _log_path(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    py_path = [str(code_root), str(code_root / "lib")]
    if env.get("PYTHONPATH"):
        py_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_path)
    env["CCB_TELEGRAM_PROJECT_ROOT"] = str(project_root)

    # Avoid raw fork() on macOS. Objective-C / SystemConfiguration work inside the
    # child (for example proxy lookup during urllib calls) can abort with
    # "crashed on child side of fork pre-exec". Launch a detached subprocess instead.
    if os.name == "nt" or sys.platform == "darwin":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        with log_path.open("a", buffering=1) as log_file:
            kwargs = {
                "args": [sys.executable, "-m", "telegram_bridge.daemon", "run"],
                "stdin": subprocess.DEVNULL,
                "stdout": log_file,
                "stderr": log_file,
                "cwd": str(code_root),
                "close_fds": True,
                "env": env,
            }
            if os.name == "nt":
                kwargs["creationflags"] = creationflags
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(**kwargs)
        print(f"[telegramd] Started in background (PID: {proc.pid})")
        return

    pid = os.fork()
    if pid > 0:
        print(f"[telegramd] Started in background (PID: {pid})")
        return
    os.setsid()
    os.umask(0)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = sys.stdout
    daemon.start()


def stop_daemon(work_dir: str | Path | None = None) -> bool:
    state = read_daemon_state(work_dir)
    if not state:
        print("Telegram daemon is not running")
        return False
    if not _is_process_alive(state.pid):
        print("Telegram daemon is not running")
        remove_daemon_state(work_dir)
        return False
    try:
        os.kill(state.pid, signal.SIGTERM)
        for _ in range(20):
            if not _is_process_alive(state.pid):
                remove_daemon_state(work_dir)
                print("Telegram daemon stopped")
                return True
            time.sleep(0.25)
        print("Warning: Telegram daemon did not stop gracefully")
        return False
    except Exception:
        remove_daemon_state(work_dir)
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    project_root = os.environ.get("CCB_TELEGRAM_PROJECT_ROOT") or os.getcwd()
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        TelegramDaemon(load_config(project_root), project_root).start()
        return 0
    if cmd == "start":
        start_daemon(foreground="--foreground" in argv[1:], work_dir=project_root)
        return 0
    if cmd == "stop":
        return 0 if stop_daemon(project_root) else 1
    if cmd == "status":
        print(json.dumps(get_daemon_status(project_root), ensure_ascii=False, indent=2))
        return 0
    print("Usage: python -m telegram_bridge.daemon [run|start|stop|status]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
