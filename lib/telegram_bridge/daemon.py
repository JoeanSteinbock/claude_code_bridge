from __future__ import annotations

import json
import os
import re
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

    def _download_attachments(self, msg: dict) -> list[Path]:
        """Download all supported attachments from a Telegram message.

        Returns the list of local file paths (empty if no attachments).
        Silently ignores unsupported types (sticker/location/etc.).
        """
        out: list[Path] = []
        dest = self._downloads_dir()

        # photo: largest variant last
        photos = msg.get("photo")
        if isinstance(photos, list) and photos:
            biggest = max(photos, key=lambda p: int(p.get("file_size") or 0)) if any(isinstance(p, dict) for p in photos) else None
            if isinstance(biggest, dict):
                file_id = str(biggest.get("file_id") or "")
                if file_id:
                    mid = int(msg.get("message_id") or 0)
                    out.append(self.client.download_file(file_id, dest, preferred_name=f"photo-{mid}.jpg"))

        # document, voice, audio, video, video_note, animation — single file each
        for key in ("document", "voice", "audio", "video", "video_note", "animation"):
            media = msg.get(key)
            if not isinstance(media, dict):
                continue
            file_id = str(media.get("file_id") or "")
            if not file_id:
                continue
            name = str(media.get("file_name") or "").strip()
            out.append(self.client.download_file(file_id, dest, preferred_name=name))

        return out

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

        # Detect and download any attachments. If present, we append a
        # machine-parseable "[attachment]" line per file to the message so the
        # provider can open it with its own file-reading tool.
        attachments: list[Path] = []
        try:
            attachments = self._download_attachments(msg)
        except Exception as exc:
            _write_log(f"[telegramd] attachment error chat={chat_id}: {exc}", self.project_root)
            self._send_text(chat_id, f"⚠️ couldn't fetch attachment: {exc}", reply_to_message_id=reply_to)
            return

        if attachments:
            atts = "\n".join(f"[attachment] {p}" for p in attachments)
            text = f"{text}\n\n{atts}".strip() if text else atts

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
        if not parsed.message:
            self._send_text(chat_id, "Empty message.", reply_to_message_id=reply_to)
            return

        if parsed.broadcast:
            providers = list(self.config.broadcast_providers)
        else:
            # Precedence: explicit prefix > reply_to target > default_provider.
            inferred = _provider_from_replied_to(msg.get("reply_to_message"))
            chosen = parsed.provider or inferred or self.config.default_provider
            providers = [chosen]
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
        self._run_request(provider, combined, chat_id, anchor_id)

    def _run_request(self, provider: str, message: str, chat_id: str, reply_to_message_id: int) -> None:
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

        try:
            result = subprocess.run(
                [ask_cmd, provider, "--foreground", "--timeout", str(self.config.request_timeout_seconds)],
                cwd=work_dir,
                env=env,
                input=message,
                capture_output=True,
                text=True,
                timeout=self.config.request_timeout_seconds + 30,
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
