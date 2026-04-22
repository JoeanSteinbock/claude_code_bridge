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
}

# Matches the `[Provider]` prefix our bot prepends to every reply
# (see _run_request). Used to infer routing when a user taps reply.
_PROVIDER_PREFIX_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*)\]")


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


def _provider_from_replied_to(reply_to_message: dict | None) -> str | None:
    """If the user replied to one of our `[Provider]` messages, return that provider."""
    if not isinstance(reply_to_message, dict):
        return None
    body = str(reply_to_message.get("text", "") or reply_to_message.get("caption", "") or "").lstrip()
    m = _PROVIDER_PREFIX_RE.match(body)
    if not m:
        return None
    candidate = m.group(1).lower()
    return candidate if candidate in SUPPORTED_PROVIDERS else None


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
                {"command": "providers", "description": "list available providers"},
                {"command": "help", "description": "show usage"},
            ])
        except Exception as exc:
            _write_log(f"[telegramd] setMyCommands failed: {exc}", self.project_root)

        _write_log(f"[telegramd] started pid={self.state.pid} bot=@{username or 'unknown'}", self.project_root)
        while not self.stop_event.is_set():
            try:
                updates = self.client.get_updates(
                    offset=(self.state.last_update_id + 1) if self.state.last_update_id else None,
                    timeout=self.config.long_poll_timeout_seconds,
                )
                if not updates:
                    time.sleep(self.config.polling_interval_seconds)
                    continue
                for update in updates:
                    self._handle_update(update)
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

    def _handle_update(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0) or 0)
        if update_id > self.state.last_update_id:
            self.state.last_update_id = update_id
            write_daemon_state(self.state, self.project_root)

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
        if parsed.command in ("context", "compact", "status"):
            self._run_slash_passthrough(parsed.command, parsed.provider or "claude",
                                        chat_id, reply_to)
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
            inferred = _provider_from_replied_to(msg.get("reply_to_message"))
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
        env.pop("CCB_PARENT_PID", None)
        env.pop("CCB_MANAGED", None)
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

    def _run_slash_passthrough(
        self,
        command: str,
        provider: str,
        chat_id: str,
        reply_to_message_id: int,
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
            subprocess.run(["tmux", "send-keys", "-t", pane_id, f"/{command}"],
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
